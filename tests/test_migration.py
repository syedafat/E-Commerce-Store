import mongomock


def test_product_migration_is_dry_run_by_default_and_idempotent(monkeypatch):
    from scripts import migrate_products
    db = mongomock.MongoClient().db
    db.products.insert_one({
        "name": "Old item", "category": "Home", "price": "8.50",
        "quantity": "6", "cost_price": 3,
    })
    monkeypatch.setattr(migrate_products, "db", db)
    monkeypatch.setattr(migrate_products, "product_info_collection", db.product_information)
    assert migrate_products.migrate(apply=False) == (1, 0, 0, 0, 0)
    assert db.product_information.count_documents({}) == 0
    assert migrate_products.migrate(apply=True) == (1, 0, 0, 0, 0)
    product = db.product_information.find_one({})
    assert product["product_name"] == "Old item"
    assert product["available_quantity"] == 6
    assert migrate_products.migrate(apply=True) == (0, 1, 0, 0, 0)


def test_migration_reports_but_retains_exact_duplicate_candidate(monkeypatch):
    from scripts import migrate_products
    db = mongomock.MongoClient().db
    db.product_information.insert_one({
        "product_name": "Same", "category": "C", "price": 5,
        "available_quantity": 2,
    })
    db.products.insert_one({
        "name": "same", "category": "c", "price": 5,
        "quantity": 2,
    })
    monkeypatch.setattr(migrate_products, "db", db)
    monkeypatch.setattr(migrate_products, "product_info_collection", db.product_information)
    result = migrate_products.migrate(apply=True)
    assert result == (1, 0, 0, 1, 3)
    assert db.product_information.count_documents({}) == 2


def test_migration_normalizes_convertible_legacy_numeric_fields_only(monkeypatch):
    from scripts import migrate_products
    db = mongomock.MongoClient().db
    product_id = db.product_information.insert_one({
        "name": "String stock", "selling_price": "9.50",
        "available_quantity": "7", "category": "Home",
    }).inserted_id
    monkeypatch.setattr(migrate_products, "db", db)
    monkeypatch.setattr(migrate_products, "product_info_collection", db.product_information)
    assert migrate_products.migrate(apply=False) == (0, 0, 0, 0, 6)
    before = db.product_information.find_one({"_id": product_id})
    assert "product_name" not in before and "price" not in before
    migrate_products.migrate(apply=True)
    product = db.product_information.find_one({"_id": product_id})
    assert product["product_name"] == "String stock"
    assert product["price"] == 9.5
    assert product["available_quantity"] == 7
    assert product["selling_price"] == "9.50"  # original source field preserved
