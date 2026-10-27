from flask import Blueprint, abort, render_template
from pymongo.errors import PyMongoError

from authz import admin_required
from models import product_info_collection

product_information_bp = Blueprint("product_information", __name__)


@product_information_bp.get("/product-information")
@admin_required
def product_information():
    """Compatibility catalog view. Product create/edit uses the canonical CRUD workflow."""
    try:
        products = list(product_info_collection.find({"is_archived": {"$ne": True}}).sort("product_name", 1).limit(100))
    except PyMongoError:
        abort(503)
    return render_template("product_information.html", products=products)
