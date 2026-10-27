from bson import ObjectId
from pymongo.errors import OperationFailure
from werkzeug.security import check_password_hash, generate_password_hash

from tests.conftest import signed_in


def test_public_registration_cannot_choose_admin(client, app):
    from routes import auth
    response = client.get("/register")
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    response = client.post("/register", data={
        "csrf_token": token, "name": "Sam", "email": "sam@example.com",
        "password": "a-long-password-123", "role": "admin",
    })
    assert response.status_code == 302
    user = auth.user_collection.find_one({"email": "sam@example.com"})
    assert user["role"] == "user"
    assert "password" not in user
    assert user["password_hash"] != "a-long-password-123"


def test_post_requires_csrf(client):
    assert client.post("/logout").status_code == 400


def test_admin_routes_require_admin(client):
    assert client.get("/admin").status_code == 302
    signed_in(client, "user")
    assert client.get("/admin").status_code == 403


def test_admin_role_is_rechecked_after_session_creation(client):
    from models import user_collection
    signed_in(client, "admin")
    with client.session_transaction() as sess:
        user_id = ObjectId(sess["user_id"])
    user_collection.update_one({"_id": user_id}, {"$set": {"role": "user"}})
    assert client.get("/admin").status_code == 403


def test_admin_creation_is_cli_only(client, app, monkeypatch):
    import app as app_module
    from routes import auth
    monkeypatch.setattr(app_module, "database_is_available", lambda: True)
    assert client.get("/create-admin").status_code == 404
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-admin",
        "--email", "new-admin@example.test", "--name", "New Admin"],
        input="a-secure-admin-password\na-secure-admin-password\n")
    assert result.exit_code == 0, result.output
    user = auth.user_collection.find_one({"email": "new-admin@example.test"})
    assert user["role"] == "admin"
    assert "password" not in user
    assert user["password_hash"] != "a-secure-admin-password"
    assert app_module.admin_collection.find_one({"user_id": user["_id"]})


def test_init_db_refuses_duplicate_emails_without_modifying_records(app, monkeypatch):
    import app as app_module
    users = app_module.user_collection
    users.insert_many([
        {"name": "First", "email": "duplicate@example.test", "role": "user"},
        {"name": "Second", "email": "duplicate@example.test", "role": "admin"},
    ])
    monkeypatch.setattr(app_module, "database_is_available", lambda: True)
    called = []
    monkeypatch.setattr(app_module, "initialize_indexes", lambda: called.append(True))
    result = app.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code != 0
    assert "check-duplicate-emails" in result.output
    assert users.count_documents({"email": "duplicate@example.test"}) == 2
    assert not called


def test_duplicate_email_diagnostic_is_read_only(app, monkeypatch):
    import app as app_module
    app_module.user_collection.insert_many([
        {"name": "First", "email": "same@example.test"},
        {"name": "Second", "email": "same@example.test"},
    ])
    monkeypatch.setattr(app_module, "database_is_available", lambda: True)
    result = app.test_cli_runner().invoke(args=["check-duplicate-emails"])
    assert result.exit_code == 0
    assert "same@example.test: 2 records" in result.output
    assert app_module.user_collection.count_documents({"email": "same@example.test"}) == 2


def test_account_inspection_never_outputs_password_fields(app, monkeypatch):
    import app as app_module
    app_module.user_collection.insert_one({
        "name": "Safe Inspect", "email": "inspect@example.test", "role": "user",
        "password_hash": "never-print-this-hash",
    })
    monkeypatch.setattr(app_module, "database_is_available", lambda: True)
    result = app.test_cli_runner().invoke(args=["inspect-user-email", "--email", "inspect@example.test"])
    assert result.exit_code == 0
    assert "Safe Inspect" in result.output
    assert "never-print-this-hash" not in result.output
    assert "password_hash" not in result.output


def test_cli_password_reset_hashes_existing_unique_account(app, monkeypatch):
    import app as app_module
    user_id = app_module.user_collection.insert_one({
        "name": "Reset User", "email": "reset@example.test", "password_hash": generate_password_hash("old-long-password"),
    }).inserted_id
    monkeypatch.setattr(app_module, "database_is_available", lambda: True)
    result = app.test_cli_runner().invoke(
        args=["reset-user-password", "--email", "reset@example.test"],
        input="y\nnew-long-password\nnew-long-password\n",
    )
    assert result.exit_code == 0, result.output
    updated = app_module.user_collection.find_one({"_id": user_id})
    assert check_password_hash(updated["password_hash"], "new-long-password")
    assert "new-long-password" not in result.output


def test_login_fails_closed_for_duplicate_email_records(client):
    from routes import auth
    auth.user_collection.insert_many([
        {"name": "First", "email": "same@example.test", "password_hash": generate_password_hash("long-password-one")},
        {"name": "Second", "email": "same@example.test", "password_hash": generate_password_hash("long-password-two")},
    ])
    client.get("/")
    with client.session_transaction() as sess:
        csrf_token = sess["csrf_token"]
    response = client.post("/", data={"csrf_token": csrf_token,
                                      "email": "same@example.test", "password": "long-password-one"})
    assert response.status_code == 200
    assert b"needs administrator review" in response.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_existing_account_can_only_be_granted_admin_from_cli(app, monkeypatch):
    import app as app_module
    user_id = app_module.user_collection.insert_one({
        "name": "Reviewed", "email": "reviewed@example.test",
        "password_hash": generate_password_hash("long-account-password"), "role": "user",
    }).inserted_id
    monkeypatch.setattr(app_module, "database_is_available", lambda: True)
    result = app.test_cli_runner().invoke(args=["grant-admin", "--email", "reviewed@example.test"], input="y\n")
    assert result.exit_code == 0, result.output
    assert app_module.user_collection.find_one({"_id": user_id})["role"] == "admin"
    assert app_module.admin_collection.find_one({"user_id": user_id})


def test_legacy_public_admin_role_without_grant_is_not_authorized(client):
    from routes import auth
    auth.user_collection.insert_one({
        "name": "Legacy Admin", "email": "legacy-admin@example.test",
        "password_hash": generate_password_hash("legacy-admin-password"),
        "role": "admin",
    })
    client.get("/")
    with client.session_transaction() as sess:
        csrf_token = sess["csrf_token"]
    login = client.post("/", data={"csrf_token": csrf_token,
                                   "email": "legacy-admin@example.test",
                                   "password": "legacy-admin-password"})
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/user")
    assert client.get("/admin").status_code == 403


def test_legacy_plaintext_password_is_upgraded_on_successful_login(client):
    from routes import auth
    auth.user_collection.insert_one({
        "name": "Legacy", "email": "legacy@example.test",
        "password": "old-secure-password", "role": "user",
    })
    client.get("/")
    with client.session_transaction() as sess:
        csrf_token = sess["csrf_token"]
    response = client.post("/", data={
        "csrf_token": csrf_token, "email": "legacy@example.test",
        "password": "old-secure-password",
    })
    assert response.status_code == 302
    user = auth.user_collection.find_one({"email": "legacy@example.test"})
    assert "password" not in user
    assert check_password_hash(user["password_hash"], "old-secure-password")


def test_login_rate_limit_applies_to_failed_posts(client):
    client.get("/")
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    responses = [client.post("/", data={"csrf_token": token, "email": "nobody@example.test", "password": "bad"})
                 for _ in range(11)]
    assert all(response.status_code == 200 for response in responses[:10])
    assert responses[10].status_code == 429


def test_login_ignores_external_next_redirect(client, app):
    from routes import auth
    auth.user_collection.insert_one({
        "name": "Regular", "email": "redirect@example.test",
        "password_hash": generate_password_hash("regular-account-password"), "role": "user",
    })
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    client.get("/?next=https://attacker.invalid")
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    response = client.post("/?next=https://attacker.invalid", data={
        "csrf_token": token, "email": "redirect@example.test", "password": "regular-account-password",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/user")


def test_sale_validates_and_deducts_stock_once(client):
    from routes import sales_management
    product_id = ObjectId()
    sales_management.product_info_collection.insert_one({
        "_id": product_id, "product_name": "Widget", "price": 10.0,
        "cost_price": 4.0, "available_quantity": 3,
    })
    signed_in(client)
    ok = client.post("/sales/add", data={
        "csrf_token": "test-csrf", "product_id": str(product_id),
        "quantity_sold": "2", "discount_applied": "1",
    })
    assert ok.status_code == 302
    assert sales_management.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 1
    sale = sales_management.sales_collection.find_one({"product_id": product_id})
    assert sale["total_revenue"] == 19.0
    assert sale["profit_per_sale"] == 11.0


def test_sale_rejects_negative_quantity_and_excessive_discount(client):
    from routes import sales_management
    product_id = ObjectId()
    sales_management.product_info_collection.insert_one({
        "_id": product_id, "product_name": "Widget", "price": 10.0,
        "cost_price": 4.0, "available_quantity": 3,
    })
    signed_in(client)
    for quantity, discount in (("-2", "0"), ("1", "11")):
        response = client.post("/sales/add", data={
            "csrf_token": "test-csrf", "product_id": str(product_id),
            "quantity_sold": quantity, "discount_applied": discount,
        })
        assert response.status_code == 302
    assert sales_management.sales_collection.count_documents({}) == 0
    assert sales_management.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 3


def test_stock_check_blocks_second_sale_after_last_units(client):
    from routes import sales_management
    product_id = ObjectId()
    sales_management.product_info_collection.insert_one({
        "_id": product_id, "product_name": "Limited", "price": 5,
        "cost_price": 2, "available_quantity": 1,
    })
    signed_in(client)
    payload = {"csrf_token": "test-csrf", "product_id": str(product_id),
               "quantity_sold": "1", "discount_applied": "0"}
    assert client.post("/sales/add", data=payload).status_code == 302
    assert client.post("/sales/add", data=payload).status_code == 302
    assert sales_management.sales_collection.count_documents({}) == 1
    assert sales_management.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 0


def test_sale_form_token_makes_retries_idempotent(client):
    from routes import sales_management
    product_id = _sale_product(sales_management.product_info_collection)
    signed_in(client)
    payload = _sale_form(product_id)
    payload["operation_id"] = "a" * 32
    assert client.post("/sales/add", data=payload).status_code == 302
    assert client.post("/sales/add", data=payload).status_code == 302
    assert sales_management.sales_collection.count_documents({"operation_id": "a" * 32}) == 1
    assert sales_management.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 1


def test_archived_product_cannot_be_sold(client):
    from routes import sales_management
    product_id = sales_management.product_info_collection.insert_one({
        "product_name": "Archived", "price": 5, "cost_price": 2,
        "available_quantity": 3, "is_archived": True,
    }).inserted_id
    signed_in(client)
    response = client.post("/sales/add", data=_sale_form(product_id))
    assert response.status_code == 302
    assert sales_management.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 3
    assert sales_management.sales_collection.count_documents({}) == 0


def test_inventory_stock_form_rejects_negative_values(client):
    from routes import inventory
    product_id = ObjectId()
    inventory.product_info_collection.insert_one({"_id": product_id, "available_quantity": 4})
    signed_in(client)
    response = client.post(f"/inventory/update-stock/{product_id}", data={
        "csrf_token": "test-csrf", "quantity": "-2", "update_type": "add",
    })
    assert response.status_code == 302
    assert inventory.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 4


def test_inventory_adjustment_retry_with_same_token_is_idempotent(client):
    from routes import inventory
    product_id = inventory.product_info_collection.insert_one({"product_name": "Retry", "available_quantity": 4}).inserted_id
    signed_in(client)
    data = {"csrf_token": "test-csrf", "operation_id": "b"*32, "quantity": "3",
            "update_type": "add", "reason": "Delivery"}
    assert client.post(f"/inventory/update-stock/{product_id}", data=data).status_code == 302
    assert client.post(f"/inventory/update-stock/{product_id}", data=data).status_code == 302
    assert inventory.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 7


class InsertFails:
    def __init__(self, collection, persist_first=False):
        self.collection = collection
        self.persist_first = persist_first

    def insert_one(self, document, **kwargs):
        if self.persist_first:
            self.collection.insert_one(document, **kwargs)
        raise OperationFailure("simulated insert failure")

    def __getattr__(self, name):
        return getattr(self.collection, name)


def _sale_product(collection):
    product_id = ObjectId()
    collection.insert_one({
        "_id": product_id, "product_name": "Widget", "price": 10,
        "cost_price": 4, "available_quantity": 3,
    })
    return product_id


def _sale_form(product_id):
    return {"csrf_token": "test-csrf", "product_id": str(product_id),
            "quantity_sold": "2", "discount_applied": "0"}


def test_sale_insert_failure_restores_stock(client, monkeypatch):
    from routes import sales_management
    product_id = _sale_product(sales_management.product_info_collection)
    actual_sales = sales_management.sales_collection
    monkeypatch.setattr(sales_management, "sales_collection", InsertFails(actual_sales))
    signed_in(client)
    response = client.post("/sales/add", data=_sale_form(product_id))
    assert response.status_code == 302
    assert sales_management.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 3
    assert actual_sales.count_documents({}) == 0


def test_failed_stock_restore_creates_reconciliation_record(client, monkeypatch):
    from routes import sales_management
    product_id = _sale_product(sales_management.product_info_collection)
    actual_products = sales_management.product_info_collection

    class RestoreFails:
        calls = 0

        def update_one(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise OperationFailure("simulated restore failure")
            return actual_products.update_one(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(actual_products, name)

    monkeypatch.setattr(sales_management, "product_info_collection", RestoreFails())
    monkeypatch.setattr(sales_management, "sales_collection", InsertFails(sales_management.sales_collection))
    signed_in(client)
    response = client.post("/sales/add", data=_sale_form(product_id))
    assert response.status_code == 503
    assert actual_products.find_one({"_id": product_id})["available_quantity"] == 1
    incident = sales_management.sales_reconciliation_collection.find_one({"product_id": product_id})
    assert incident["reason"] == "stock_restore_failed"
    assert incident["quantity"] == 2


def test_ambiguous_insert_that_persisted_is_not_rolled_back(client, monkeypatch):
    from routes import sales_management
    product_id = _sale_product(sales_management.product_info_collection)
    actual_sales = sales_management.sales_collection
    monkeypatch.setattr(sales_management, "sales_collection", InsertFails(actual_sales, persist_first=True))
    signed_in(client)
    response = client.post("/sales/add", data=_sale_form(product_id))
    assert response.status_code == 302
    assert actual_sales.count_documents({}) == 1
    assert sales_management.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 1


def test_production_refuses_sales_without_transaction_support(client, monkeypatch, app):
    from routes import sales_management
    product_id = _sale_product(sales_management.product_info_collection)
    monkeypatch.setattr(sales_management, "transactions_supported", lambda: False)
    app.config["MONGO_REQUIRE_TRANSACTIONS"] = True
    signed_in(client)
    response = client.post("/sales/add", data=_sale_form(product_id))
    assert response.status_code == 503
    assert sales_management.product_info_collection.find_one({"_id": product_id})["available_quantity"] == 3
    assert sales_management.sales_collection.count_documents({}) == 0


def test_supported_mongodb_uses_one_session_for_sale_and_stock(client, monkeypatch):
    from routes import sales_management
    product_id = _sale_product(sales_management.product_info_collection)
    product_collection = sales_management.product_info_collection
    sales_collection = sales_management.sales_collection
    observed_sessions = []

    class Context:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class Session(Context):
        def __enter__(self):
            return self

        def start_transaction(self):
            return Context()

    class FakeClient:
        def start_session(self):
            return Session()

    class SessionAwareCollection:
        def __init__(self, collection):
            self.collection = collection

        def update_one(self, *args, session=None, **kwargs):
            observed_sessions.append(session)
            return self.collection.update_one(*args, **kwargs)

        def insert_one(self, *args, session=None, **kwargs):
            observed_sessions.append(session)
            return self.collection.insert_one(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.collection, name)

    monkeypatch.setattr(sales_management, "transactions_supported", lambda: True)
    monkeypatch.setattr(sales_management, "mongo_client", FakeClient())
    monkeypatch.setattr(sales_management, "product_info_collection", SessionAwareCollection(product_collection))
    monkeypatch.setattr(sales_management, "sales_collection", SessionAwareCollection(sales_collection))
    signed_in(client)
    response = client.post("/sales/add", data=_sale_form(product_id))
    assert response.status_code == 302
    assert sales_collection.count_documents({}) == 1
    assert product_collection.find_one({"_id": product_id})["available_quantity"] == 1
    assert len(observed_sessions) == 2 and observed_sessions[0] is observed_sessions[1]
