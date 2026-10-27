from datetime import datetime, timezone
import math
import re

from bson import ObjectId
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from pymongo.errors import PyMongoError

from authz import admin_required
from models import product_collection, sales_collection

product_bp = Blueprint("product", __name__)


def _id(value):
    if not ObjectId.is_valid(value):
        abort(404)
    return ObjectId(value)


def _product_values(form):
    name = form.get("product_name", form.get("name", "")).strip()
    category = form.get("category", "").strip()
    description = form.get("description", "").strip()
    product_id = form.get("product_id", "").strip()
    try:
        selling_price = float(form.get("selling_price", form.get("price", "")))
        cost = float(form.get("cost_price", 0) or 0)
        quantity = int(form.get("available_quantity", form.get("quantity", "0")))
        discount = float(form.get("discount_percentage", 0) or 0)
        reorder_level = int(form.get("reorder_level", 5) or 0)
    except (TypeError, ValueError):
        raise ValueError("Enter valid price, discount, and stock values.")
    if not name or len(name) > 150 or not category or len(category) > 100:
        raise ValueError("Product name and category are required.")
    if not all(math.isfinite(value) for value in (selling_price, cost, discount)) or selling_price < 0 or cost < 0 or quantity < 0 or reorder_level < 0 or not 0 <= discount <= 100:
        raise ValueError("Prices and stock must be non-negative; discount must be 0–100%.")
    price = round(selling_price * (1-discount/100), 2)
    return {
        "product_name": name, "price": price, "selling_price": selling_price,
        "cost_price": cost, "available_quantity": quantity,
        "category": category, "description": description,
        "discount_percentage": discount,
        "reorder_level": reorder_level,
        "product_id": product_id or None,
        "image_url": form.get("image_url", "").strip(),
    }


@product_bp.get("/products")
@admin_required
def products():
    search = request.args.get("q", "").strip()[:100]
    category = request.args.get("category", "").strip()[:100]
    stock = request.args.get("stock", "")
    sort = request.args.get("sort", "product_name")
    sort = sort if sort in {"product_name", "category", "price", "available_quantity", "created_at"} else "product_name"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    query = {}
    query["is_archived"] = {"$ne": True}
    if search:
        query["$or"] = [{"product_name": {"$regex": re.escape(search), "$options": "i"}},
                        {"product_id": {"$regex": re.escape(search), "$options": "i"}}]
    if category:
        query["category"] = category
    if stock == "low":
        query["$expr"] = {"$and": [{"$gt": ["$available_quantity", 0]},
                                   {"$lte": ["$available_quantity", {"$ifNull": ["$reorder_level", 5]}]}]}
    elif stock == "out":
        query["available_quantity"] = {"$lte": 0}
    elif stock == "available":
        query["available_quantity"] = {"$gt": 0}
    try:
        total = product_collection.count_documents(query)
        all_products = list(product_collection.find(query).sort(sort, 1).skip((page - 1) * 25).limit(25))
        categories = sorted(value for value in product_collection.distinct("category") if value)
    except PyMongoError:
        abort(503)
    return render_template("products.html", products=all_products, total=total, page=page,
                           pages=max(1, (total + 24) // 25), categories=categories,
                           filters={"q": search, "category": category, "stock": stock, "sort": sort})


@product_bp.get("/product-info/<product_id>")
@admin_required
def product_info(product_id):
    try:
        product = product_collection.find_one({"_id": _id(product_id)})
    except PyMongoError:
        abort(503)
    if not product:
        abort(404)
    return render_template("product_info.html", product=product)


@product_bp.route("/add-product", methods=["GET", "POST"])
@admin_required
def add_product():
    if request.method == "POST":
        try:
            product = _product_values(request.form)
            if product["product_id"] and product_collection.find_one({"product_id": product["product_id"]}):
                raise ValueError("That product ID is already in use.")
            now = datetime.now(timezone.utc)
            product.update({"created_at": now, "updated_at": now})
            product_collection.insert_one(product)
            flash("Product added.", "success")
            return redirect(url_for("product.products"))
        except ValueError as exc:
            flash(str(exc), "error")
        except PyMongoError:
            flash("Could not save the product. Please try again.", "error")
    return render_template("add_product.html")


@product_bp.route("/edit-product/<product_id>", methods=["GET", "POST"])
@admin_required
def edit_product(product_id):
    oid = _id(product_id)
    try:
        product = product_collection.find_one({"_id": oid})
    except PyMongoError:
        abort(503)
    if not product:
        abort(404)
    if request.method == "POST":
        try:
            values = _product_values(request.form)
            if values["product_id"] and product_collection.find_one({"product_id": values["product_id"], "_id": {"$ne": oid}}):
                raise ValueError("That product ID is already in use.")
            values["updated_at"] = datetime.now(timezone.utc)
            product_collection.update_one({"_id": oid}, {"$set": values})
            flash("Product updated.", "success")
            return redirect(url_for("product.products"))
        except ValueError as exc:
            flash(str(exc), "error")
        except PyMongoError:
            abort(503)
    return render_template("edit_product.html", product=product)


@product_bp.post("/delete-product/<product_id>")
@admin_required
def delete_product(product_id):
    try:
        oid = _id(product_id)
        product = product_collection.find_one({"_id": oid})
        if not product:
            abort(404)
        if sales_collection.find_one({"$or": [{"product_id": oid},
                                                {"product_name": product.get("product_name", "")}]}):
            flash("This product has sales history and cannot be deleted. Set its stock to zero or archive it instead.", "error")
            return redirect(url_for("product.products"))
        result = product_collection.update_one({"_id": oid, "is_archived": {"$ne": True}},
                                               {"$set": {"is_archived": True, "archived_at": datetime.now(timezone.utc),
                                                          "updated_at": datetime.now(timezone.utc)}})
    except PyMongoError:
        abort(503)
    if result.matched_count == 0:
        abort(404)
    flash("Product archived. Its history and stock record are preserved.", "success")
    return redirect(url_for("product.products"))
