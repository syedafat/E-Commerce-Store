"""Shared MongoDB client and collection handles."""
from datetime import datetime, timezone
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from config import Config

Config.validate()
client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[Config.MONGO_DB_NAME]
user_collection = db["users_auth"]
admin_collection = db["admin_accounts"]
# product_information is the canonical collection; product_collection remains
# an alias for callers during migration so every product route uses one store.
product_info_collection = db["product_information"]
product_collection = product_info_collection
inventory_collection = db["inventory"]
sales_collection = db["sales_records"]
sales_reconciliation_collection = db["sales_reconciliation"]
inventory_movements_collection = db["inventory_movements"]


def initialize_indexes():
    """Create safe, useful indexes. This does not delete or rewrite records."""
    user_collection.create_index("email", unique=True, name="users_email_unique")
    admin_collection.create_index("user_id", unique=True, name="admin_user_unique")
    product_info_collection.create_index("product_id", sparse=True, name="product_id_lookup")
    product_info_collection.create_index("migration_key", unique=True, sparse=True, name="product_migration_key")
    sales_collection.create_index([("sale_date", -1)], name="sales_date_desc")
    sales_collection.create_index("operation_id", unique=True, sparse=True, name="sales_operation_unique")
    sales_reconciliation_collection.create_index([("created_at", -1)], name="sales_reconciliation_date_desc")
    inventory_movements_collection.create_index([("product_id", 1), ("created_at", -1)], name="inventory_movement_product_date")
    inventory_movements_collection.create_index("movement_id", unique=True, sparse=True, name="inventory_movement_id_unique")


def database_is_available():
    try:
        client.admin.command("ping")
        return True
    except PyMongoError:
        return False


def recent_inventory_movements(limit=250):
    """Return recent movement copies plus embedded audit entries if a mirror write failed."""
    copied = list(inventory_movements_collection.find().sort("created_at", -1).limit(limit))
    seen = {row.get("movement_id") for row in copied if row.get("movement_id")}
    # Each product keeps its latest 100 audit events atomically with stock updates.
    embedded = product_info_collection.find({"inventory_history.0": {"$exists": True}},
                                            {"inventory_history": 1}).limit(1000)
    for product in embedded:
        for row in product.get("inventory_history", []):
            if row.get("movement_id") not in seen:
                copied.append(row)
                seen.add(row.get("movement_id"))
    copied.sort(key=lambda row: row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return copied[:limit]
