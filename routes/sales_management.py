from datetime import date, datetime, time, timedelta, timezone
import math
import uuid

from bson import ObjectId
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from pymongo.errors import DuplicateKeyError, PyMongoError

from authz import admin_required
from models import (
    client as mongo_client,
    product_info_collection,
    sales_collection,
    sales_reconciliation_collection,
)

sales_bp = Blueprint("sales", __name__)


def transactions_supported():
    """Return whether this MongoDB deployment advertises replica-set or mongos support."""
    try:
        hello = mongo_client.admin.command("hello")
        return bool(hello.get("setName") or hello.get("msg") == "isdbgrid")
    except (PyMongoError, NotImplementedError):
        return False


def _record_reconciliation(operation_id, product_id, quantity, reason):
    entry = {
        "operation_id": operation_id,
        "product_id": product_id,
        "quantity": quantity,
        "reason": reason,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        sales_reconciliation_collection.insert_one(entry)
        current_app.logger.critical(
            "Sale operation requires reconciliation: operation_id=%s product_id=%s quantity=%s reason=%s",
            operation_id, product_id, quantity, reason,
        )
    except PyMongoError:
        current_app.logger.critical(
            "Sale reconciliation record could not be persisted: operation_id=%s product_id=%s quantity=%s reason=%s",
            operation_id, product_id, quantity, reason,
        )


def _sale_was_recorded(operation_id):
    try:
        return sales_collection.find_one({"operation_id": operation_id}) is not None
    except PyMongoError:
        return None


def _redirect_with_failure(message):
    flash(message, "error")
    return redirect(url_for("sales.view_sales"))


@sales_bp.get("/sales")
@admin_required
def view_sales():
    today = datetime.now(timezone.utc).date()
    try:
        start_date = date.fromisoformat(request.args["start"]) if request.args.get("start") else None
        end_date = date.fromisoformat(request.args["end"]) if request.args.get("end") else None
        page = max(1, int(request.args.get("page", "1")))
        if start_date and end_date and end_date < start_date:
            raise ValueError
    except ValueError:
        abort(400, description="Choose a valid sales date range.")
    query = {}
    date_clause = {}
    if start_date:
        date_clause["$gte"] = datetime.combine(start_date, time.min, timezone.utc)
    if end_date:
        date_clause["$lt"] = datetime.combine(end_date + timedelta(days=1), time.min, timezone.utc)
    if date_clause:
        query["sale_date"] = date_clause
    search = request.args.get("q", "").strip()[:100]
    if search:
        clauses = [{"product_name": {"$regex": __import__("re").escape(search), "$options": "i"}}]
        if ObjectId.is_valid(search):
            clauses.append({"_id": ObjectId(search)})
        query["$and"] = [{"$or": clauses}]
    try:
        total = sales_collection.count_documents(query)
        sales_records = list(sales_collection.find(query).sort("sale_date", -1).skip((page-1)*25).limit(25))
        products = list(product_info_collection.find({"available_quantity": {"$gt": 0}, "is_archived": {"$ne": True}}).sort("product_name", 1).limit(500))
    except PyMongoError:
        abort(503)
    return render_template("sales_management.html", sales=sales_records, products=products,
                           total=total, page=page, pages=max(1,(total+24)//25),
                           start_date=start_date.isoformat() if start_date else "",
                           end_date=end_date.isoformat() if end_date else "", search=search,
                           sale_token=uuid.uuid4().hex)


@sales_bp.get("/sales/<sale_id>")
@admin_required
def sale_detail(sale_id):
    if not ObjectId.is_valid(sale_id):
        abort(404)
    try:
        sale = sales_collection.find_one({"_id": ObjectId(sale_id)})
    except PyMongoError:
        abort(503)
    if not sale:
        abort(404)
    return render_template("sale_detail.html", sale=sale)


@sales_bp.post("/sales/add")
@admin_required
def add_sale():
    operation_id = request.form.get("operation_id", "")
    if not operation_id:
        operation_id = uuid.uuid4().hex
    if len(operation_id) != 32 or any(char not in "0123456789abcdef" for char in operation_id.lower()):
        return _redirect_with_failure("Invalid sale submission token. Refresh the page and try again.")
    operation_id = operation_id.lower()
    try:
        existing_sale = sales_collection.find_one({"operation_id": operation_id})
    except PyMongoError:
        abort(503)
    if existing_sale:
        flash(f"Sale already recorded. Total: Rs. {number_value(existing_sale.get('total_revenue')):.2f}", "success")
        return redirect(url_for("sales.view_sales"))
    product_id = request.form.get("product_id", "")
    if not ObjectId.is_valid(product_id):
        return _redirect_with_failure("Choose a valid product.")
    try:
        quantity = int(request.form.get("quantity_sold", ""))
        discount = float(request.form.get("discount_applied", "0"))
        if quantity <= 0 or not math.isfinite(discount) or discount < 0:
            raise ValueError
    except (TypeError, ValueError):
        return _redirect_with_failure("Quantity must be positive and discount cannot be negative.")

    oid = ObjectId(product_id)
    try:
        product = product_info_collection.find_one({"_id": oid, "is_archived": {"$ne": True}})
    except PyMongoError:
        abort(503)
    if not product:
        return _redirect_with_failure("Selected product was not found.")
    price = product.get("price", product.get("selling_price"))
    try:
        price = float(price)
        cost = float(product.get("cost_price", 0))
        stock = int(product.get("available_quantity", product.get("quantity", 0)))
    except (TypeError, ValueError, OverflowError):
        return _redirect_with_failure("This product has invalid price or stock data. Correct it before selling.")
    if not math.isfinite(price) or not math.isfinite(cost) or price < 0 or cost < 0 or stock < 0:
        return _redirect_with_failure("This product has invalid price or stock data.")
    if quantity > stock:
        return _redirect_with_failure(f"Insufficient stock; only {stock} units are available.")
    try:
        subtotal = round(price * quantity, 2)
        total = round(subtotal - discount, 2)
        profit = round(total - cost * quantity, 2)
    except OverflowError:
        return _redirect_with_failure("The sale values are outside the supported range.")
    if not math.isfinite(subtotal) or discount > subtotal:
        return _redirect_with_failure("Discount cannot exceed the sale subtotal.")
    if not math.isfinite(total) or not math.isfinite(profit):
        return _redirect_with_failure("The sale values are outside the supported range.")

    sale = {
        "operation_id": operation_id,
        "product_id": oid,
        "product_name": product.get("product_name", product.get("name", "")),
        "quantity_sold": quantity,
        "cost_price": cost,
        "selling_price": price,
        "subtotal": subtotal,
        "discount_applied": discount,
        "tax": 0.0,
        "total_revenue": total,
        "profit_per_sale": profit,
        "sale_date": datetime.now(timezone.utc),
    }

    use_transaction = transactions_supported()
    if current_app.config.get("MONGO_REQUIRE_TRANSACTIONS") and not use_transaction:
        current_app.logger.error("Sale refused because transaction support is required: operation_id=%s", operation_id)
        abort(503)

    if use_transaction:
        try:
            with mongo_client.start_session() as mongo_session:
                with mongo_session.start_transaction():
                    updated = product_info_collection.update_one(
                        {"_id": oid, "is_archived": {"$ne": True}, "available_quantity": {"$gte": quantity}},
                        {"$inc": {"available_quantity": -quantity}, "$set": {"updated_at": sale["sale_date"]}},
                        session=mongo_session,
                    )
                    if updated.modified_count != 1:
                        raise _InsufficientStock
                    sales_collection.insert_one(sale, session=mongo_session)
        except _InsufficientStock:
            return _redirect_with_failure("Insufficient stock; refresh the page and try again.")
        except PyMongoError:
            recorded = _sale_was_recorded(operation_id)
            if recorded is True:
                flash(f"Sale recorded. Total: Rs. {sale['total_revenue']:.2f}", "success")
                return redirect(url_for("sales.view_sales"))
            _record_reconciliation(operation_id, oid, quantity, "transaction_outcome_unknown")
            abort(503)
    else:
        try:
            updated = product_info_collection.update_one(
                {"_id": oid, "is_archived": {"$ne": True}, "available_quantity": {"$gte": quantity}},
                {"$inc": {"available_quantity": -quantity}, "$set": {"updated_at": sale["sale_date"]}},
            )
        except PyMongoError:
            _record_reconciliation(operation_id, oid, quantity, "stock_deduction_outcome_unknown")
            abort(503)
        if updated.modified_count != 1:
            return _redirect_with_failure("Insufficient stock; refresh the page and try again.")

        try:
            sales_collection.insert_one(sale)
        except DuplicateKeyError:
            # A concurrent retry with the same form token lost the unique-key race.
            try:
                product_info_collection.update_one({"_id": oid}, {"$inc": {"available_quantity": quantity}})
            except PyMongoError:
                _record_reconciliation(operation_id, oid, quantity, "idempotent_retry_stock_restore_failed")
                abort(503)
            flash("This sale was already recorded. Stock was restored for the duplicate submission.", "info")
            return redirect(url_for("sales.view_sales"))
        except PyMongoError:
            recorded = _sale_was_recorded(operation_id)
            if recorded is True:
                flash(f"Sale recorded. Total: Rs. {sale['total_revenue']:.2f}", "success")
                return redirect(url_for("sales.view_sales"))
            if recorded is None:
                _record_reconciliation(operation_id, oid, quantity, "sale_insert_outcome_unknown")
                abort(503)
            try:
                restored = product_info_collection.update_one(
                    {"_id": oid}, {"$inc": {"available_quantity": quantity}}
                )
            except PyMongoError:
                _record_reconciliation(operation_id, oid, quantity, "stock_restore_failed")
                abort(503)
            if restored.matched_count != 1:
                _record_reconciliation(operation_id, oid, quantity, "stock_restore_target_missing")
                abort(503)
            return _redirect_with_failure("Sale could not be saved; stock was restored. Please retry.")

    flash(f"Sale recorded. Total: Rs. {sale['total_revenue']:.2f}", "success")
    return redirect(url_for("sales.view_sales"))


class _InsufficientStock(Exception):
    pass


def number_value(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0
