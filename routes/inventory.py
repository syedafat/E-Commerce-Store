from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from models import product_info_collection, inventory_movements_collection, recent_inventory_movements
from bson.objectid import ObjectId
from datetime import datetime, timezone
import uuid
from authz import admin_required
from pymongo.errors import PyMongoError
from flask import abort

inventory_bp = Blueprint("inventory", __name__)

# ----------------------------- 1. VIEW INVENTORY -----------------------------
@inventory_bp.route("/inventory")
@admin_required
def view_inventory():
    # Saare products fetch kar rahe hain taaki unka current stock status dikha sakein
    try:
        query = request.args.get("q", "").strip()[:100]
        stock = request.args.get("stock", "")
        criteria = {"is_archived": {"$ne": True}}
        if query:
            import re
            criteria["$or"] = [{"product_name": {"$regex": re.escape(query), "$options": "i"}},
                               {"product_id": {"$regex": re.escape(query), "$options": "i"}}]
        if stock == "low":
            criteria["$expr"] = {"$and": [{"$gt": ["$available_quantity", 0]},
                                           {"$lte": ["$available_quantity", {"$ifNull": ["$reorder_level", 5]}]}]}
        elif stock == "out":
            criteria["available_quantity"] = {"$lte": 0}
        products = list(product_info_collection.find(criteria).sort("product_name", 1).limit(200))
        movements = recent_inventory_movements(20)
    except PyMongoError:
        abort(503)
    tokens = {str(product["_id"]): uuid.uuid4().hex for product in products}
    return render_template("inventory.html", products=products, movements=movements, inventory_tokens=tokens)


# ----------------------------- 2. ADD / UPDATE STOCK -----------------------------
# Yeh route stock badhane ya naya initial stock set karne ke kaam aayega
@inventory_bp.route("/inventory/update-stock/<product_id>", methods=["POST"])
@admin_required
def update_stock(product_id):
    if not ObjectId.is_valid(product_id):
        flash("Invalid product.", "error")
        return redirect(url_for("inventory.view_inventory"))
    operation_id = request.form.get("operation_id", "") or uuid.uuid4().hex
    if len(operation_id) != 32 or any(char not in "0123456789abcdef" for char in operation_id.lower()):
        flash("Invalid stock adjustment token. Refresh the page and retry.", "error")
        return redirect(url_for("inventory.view_inventory"))
    operation_id = operation_id.lower()
    try:
        prior = inventory_movements_collection.find_one({"movement_id": operation_id})
        if not prior:
            prior = product_info_collection.find_one({"inventory_history.movement_id": operation_id}, {"_id": 1})
    except PyMongoError:
        abort(503)
    if prior:
        flash("This stock adjustment was already recorded.", "info")
        return redirect(url_for("inventory.view_inventory"))
        
    try:
        # Form se nayi quantity ya added quantity uthayenge
        new_quantity = int(request.form.get("quantity", 0))
        update_type = request.form.get("update_type", "set") # 'set' ya 'add'
        reorder_level = request.form.get("reorder_level")
        reason = request.form.get("reason", "").strip()[:250]
    except ValueError:
        flash("Please enter a valid number for quantity.", "error")
        return redirect(url_for("inventory.view_inventory"))

    if new_quantity < 0 and update_type == "set":
        flash("Quantity cannot be negative.", "error")
        return redirect(url_for("inventory.view_inventory"))
    if new_quantity < 0 or update_type not in {"set", "add"}:
        flash("Quantity must be non-negative and update type must be valid.", "error")
        return redirect(url_for("inventory.view_inventory"))

    current_time = datetime.now(timezone.utc)
    try:
        before = product_info_collection.find_one({"_id": ObjectId(product_id), "is_archived": {"$ne": True}})
        if not before:
            flash("Product not found.", "error")
            return redirect(url_for("inventory.view_inventory"))
        old_quantity = int(before.get("available_quantity", 0))
        if reorder_level not in (None, "") and int(reorder_level) < 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Stock and reorder level must be valid non-negative integers.", "error")
        return redirect(url_for("inventory.view_inventory"))

    # FIXED: Database queries mein update timestamps add kiye hain
    if update_type == "add":
        # Purane stock mein plus karega aur timestamp update karega
        query = {
            "$inc": {"available_quantity": new_quantity},
            "$set": {"last_updated": current_time, "updated_at": current_time}
        }
    else:
        # Direct value override karega aur timestamp update karega
        query = {
            "$set": {
                "available_quantity": new_quantity,
                "last_updated": current_time, "updated_at": current_time
            }
        }
    if reorder_level not in (None, ""):
        query.setdefault("$set", {})["reorder_level"] = int(reorder_level)
    after_quantity = old_quantity + new_quantity if update_type == "add" else new_quantity
    movement = {
        "movement_id": operation_id, "product_id": ObjectId(product_id),
        "product_name": before.get("product_name", ""),
        "change": after_quantity - old_quantity, "quantity_before": old_quantity,
        "quantity_after": after_quantity,
        "reason": reason or ("Stock received" if update_type == "add" else "Stock count adjustment"),
        "created_at": current_time,
    }
    query.setdefault("$push", {})["inventory_history"] = {"$each": [movement], "$slice": -100}
    # Guard against stale counts so two concurrent manual adjustments cannot silently
    # overwrite one another. Sales continue to use their own conditional atomic decrement.
    stock_guard = {"available_quantity": old_quantity}
    if "available_quantity" not in before:
        stock_guard = {"available_quantity": {"$exists": False}}

    try:
        result = product_info_collection.update_one({"_id": ObjectId(product_id), "is_archived": {"$ne": True},
                                                     "inventory_history.movement_id": {"$ne": operation_id}, **stock_guard}, query)
        if result.matched_count and after_quantity != old_quantity:
            try:
                inventory_movements_collection.insert_one(movement)
            except PyMongoError:
                # The authoritative, capped movement is already embedded atomically
                # with the quantity update; collection copy is for efficient history queries.
                from flask import current_app
                current_app.logger.exception("Could not mirror inventory movement %s", movement["movement_id"])
    except PyMongoError:
        abort(503)
    
    if result.matched_count > 0:
        flash("Stock updated successfully!", "success")
    else:
        flash("Stock changed while this form was open. Refresh inventory and try again.", "error")
        
    return redirect(url_for("inventory.view_inventory"))


# ----------------------------- 3. DELETE INVENTORY (RESET STOCK) -----------------------------
# Inventory management mein delete ka matlab aamtaur par stock ko 0 (Out of stock) karna hota hai
@inventory_bp.route("/inventory/reset-stock/<product_id>", methods=["POST"])
@admin_required
def reset_stock(product_id):
    if not ObjectId.is_valid(product_id):
        flash("Invalid product.", "error")
        return redirect(url_for("inventory.view_inventory"))
        
    # Current formatted timestamp variable
    current_time = datetime.now(timezone.utc)

    try:
        product = product_info_collection.find_one({"_id": ObjectId(product_id), "is_archived": {"$ne": True}})
        if not product:
            flash("Product not found.", "error")
            return redirect(url_for("inventory.view_inventory"))
        old_quantity = int(product.get("available_quantity", 0))
        movement = {
            "movement_id": uuid.uuid4().hex, "product_id": ObjectId(product_id),
            "product_name": product.get("product_name", ""), "change": -old_quantity,
            "quantity_before": old_quantity, "quantity_after": 0,
            "reason": "Stock reset to zero", "created_at": current_time,
        }
        result = product_info_collection.update_one(
            {"_id": ObjectId(product_id), "is_archived": {"$ne": True}, "available_quantity": old_quantity},
            {"$set": {"available_quantity": 0, "last_updated": current_time, "updated_at": current_time},
             "$push": {"inventory_history": {"$each": [movement], "$slice": -100}}}
        )
        if result.matched_count and old_quantity:
            try:
                inventory_movements_collection.insert_one(movement)
            except PyMongoError:
                from flask import current_app
                current_app.logger.exception("Could not mirror inventory movement %s", movement["movement_id"])
    except PyMongoError:
        abort(503)
    if not result.matched_count:
        flash("Product not found.", "error")
        return redirect(url_for("inventory.view_inventory"))
    
    if result.matched_count:
        flash("Product stock has been reset to zero.", "success")
    else:
        flash("Stock changed while this form was open. Refresh and try again.", "error")
    return redirect(url_for("inventory.view_inventory"))
