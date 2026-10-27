import os

import mongomock
import pytest
from bson import ObjectId

os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough"
os.environ["MONGO_URI"] = "mongodb://127.0.0.1/test-only"
os.environ["MONGO_DB_NAME"] = "ecommerce_test"
os.environ["APP_ENV"] = "test"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["MONGO_REQUIRE_TRANSACTIONS"] = "false"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-that-is-long-enough")
    from models import client as _client  # noqa: F401
    import models
    import authz
    import app as app_module
    from routes import auth, admin_dashboard, product_management, product_information
    from routes import inventory, inventory_details, sales_management, realtime_prediction, reports

    db = mongomock.MongoClient().db
    users, products, sales, admins = db.users, db.product_information, db.sales_records, db.admin_accounts
    for module in (auth, app_module, models, authz):
        if hasattr(module, "user_collection"):
            monkeypatch.setattr(module, "user_collection", users)
        if hasattr(module, "admin_collection"):
            monkeypatch.setattr(module, "admin_collection", admins)
    monkeypatch.setattr(models, "product_info_collection", products)
    monkeypatch.setattr(models, "inventory_movements_collection", db.inventory_movements)
    for module in (admin_dashboard, product_management, product_information, inventory, inventory_details, sales_management, realtime_prediction, reports):
        if hasattr(module, "product_collection"):
            monkeypatch.setattr(module, "product_collection", products)
        if hasattr(module, "product_info_collection"):
            monkeypatch.setattr(module, "product_info_collection", products)
        if hasattr(module, "sales_collection"):
            monkeypatch.setattr(module, "sales_collection", sales)
        if hasattr(module, "sales_reconciliation_collection"):
            monkeypatch.setattr(module, "sales_reconciliation_collection", db.sales_reconciliation)
        if hasattr(module, "inventory_movements_collection"):
            monkeypatch.setattr(module, "inventory_movements_collection", db.inventory_movements)
    monkeypatch.setattr(sales_management, "transactions_supported", lambda: False)
    application = app_module.create_app({"TESTING": True, "SECRET_KEY": "test-secret-key-that-is-long-enough"})
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("csrf_token")


def signed_in(client, role="admin"):
    from models import user_collection
    from models import admin_collection
    user_id = user_collection.insert_one({"name": "Test User", "email": f"{ObjectId()}@example.test", "role": role}).inserted_id
    if role == "admin":
        admin_collection.insert_one({"user_id": user_id, "email": "test@example.test"})
    with client.session_transaction() as sess:
        sess["user_id"] = str(user_id)
        sess["user"] = "Test User"
        sess["role"] = role
        sess["csrf_token"] = "test-csrf"
