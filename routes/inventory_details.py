from flask import Blueprint, render_template, abort
from models import recent_inventory_movements
from authz import admin_required
from pymongo.errors import PyMongoError

# Naya blueprint definition
inventory_details_bp = Blueprint("inventory_details", __name__)

@inventory_details_bp.route("/inventory-details")
@admin_required
def view_all_inventory_details():
    # Saare products ka data fetch kar rahe hain details view ke liye
    try:
        movements = recent_inventory_movements(250)
    except PyMongoError:
        abort(503)
    return render_template("inventory_details.html", movements=movements)
