"""Additively migrate legacy products to the canonical product_information collection.

Run a preview first: ``python scripts/migrate_products.py``.
Apply only after reviewing the counts: ``python scripts/migrate_products.py --apply``.
The script never deletes source records and is idempotent by source id.
"""
import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models import db, product_info_collection


def convert(source, source_collection):
    price = source.get("price")
    if price is None:
        price = source.get("final_selling_price", source.get("selling_price", 0))
    quantity = source.get("available_quantity", source.get("quantity", 0))
    try:
        price = float(price or 0)
        cost = float(source.get("cost_price", 0) or 0)
        quantity = int(quantity or 0)
        reorder = int(source.get("reorder_level", 5) or 0)
        discount = float(source.get("discount_percentage", 0) or 0)
    except (ValueError, TypeError, OverflowError):
        return None
    if (not all(math.isfinite(value) for value in (price, cost, discount))
            or price < 0 or cost < 0 or quantity < 0 or reorder < 0 or not 0 <= discount <= 100):
        return None
    name = str(source.get("product_name", source.get("name", ""))).strip()
    category = str(source.get("category", "Uncategorized")).strip() or "Uncategorized"
    if not name:
        return None
    now = datetime.now(timezone.utc)
    return {
        "product_name": name, "price": price, "selling_price": price,
        "cost_price": cost, "available_quantity": quantity,
        "category": category,
        "description": str(source.get("description", "") or ""),
        "created_at": source.get("created_at") or now,
        "updated_at": source.get("updated_at") or source.get("last_updated") or now,
        "reorder_level": reorder,
        "discount_percentage": discount,
        "product_image": source.get("product_image", ""),
        "image_url": source.get("image_url", ""),
        "migration_key": f"{source_collection}:{source['_id']}",
        "migration_source": source_collection,
        "migration_source_id": str(source["_id"]),
    }


def migrate(apply=False):
    inserted = skipped = invalid = duplicate_candidates = canonical_updates = 0
    fingerprints = set()
    for canonical in product_info_collection.find({}, {
        "product_name": 1, "name": 1, "category": 1, "price": 1,
        "selling_price": 1, "final_selling_price": 1,
        "available_quantity": 1, "quantity": 1,
    }):
        try:
            name = str(canonical.get("product_name", canonical.get("name", ""))).strip().casefold()
            category = str(canonical.get("category", "Uncategorized")).strip().casefold()
            price = float(canonical.get("price", canonical.get("final_selling_price", canonical.get("selling_price", 0))) or 0)
            quantity = int(canonical.get("available_quantity", canonical.get("quantity", 0)) or 0)
            if name and math.isfinite(price):
                fingerprints.add((name, category, round(price, 2), quantity))
        except (ValueError, TypeError, OverflowError):
            continue
    for name in ("products", "product_information"):
        for source in db[name].find():
            # Existing canonical records are enriched only where a field is absent.
            if name == "product_information":
                canonical = product_info_collection.find_one({"_id": source["_id"]})
                if canonical:
                    price = canonical.get("price") or canonical.get("final_selling_price") or canonical.get("selling_price", 0)
                    quantity = canonical.get("available_quantity", canonical.get("quantity", 0))
                    defaults = {
                        "product_name": canonical.get("product_name", canonical.get("name", "")),
                        "price": price, "available_quantity": quantity,
                        "created_at": canonical.get("created_at", canonical.get("last_updated", datetime.now(timezone.utc))),
                        "updated_at": canonical.get("updated_at", canonical.get("last_updated", datetime.now(timezone.utc))),
                        "description": canonical.get("description", ""),
                    }
                    changes = {}
                    for key, value in defaults.items():
                        if key == "product_name" and not value:
                            continue
                        existing = canonical.get(key)
                        if existing is None or isinstance(existing, str) and key in {"price", "available_quantity"}:
                            try:
                                candidate = value if existing is None else existing
                                normalized = float(candidate) if key == "price" else int(candidate) if key == "available_quantity" else value
                            except (ValueError, TypeError, OverflowError):
                                continue
                            if key == "price" and (not math.isfinite(normalized) or normalized < 0):
                                continue
                            if key == "available_quantity" and normalized < 0:
                                continue
                            query = {"_id": source["_id"], key: existing} if key in canonical else {"_id": source["_id"], key: {"$exists": False}}
                            changes[key] = (query, normalized)
                    canonical_updates += len(changes)
                    if apply:
                        for key, (query, value) in changes.items():
                            product_info_collection.update_one(query, {"$set": {key: value}})
                    continue
            record = convert(source, name)
            if not record:
                invalid += 1
                continue
            exists = product_info_collection.find_one({"migration_key": record["migration_key"]}, {"_id": 1})
            if exists:
                skipped += 1
                continue
            fingerprint = (
                record["product_name"].casefold(), record["category"].casefold(),
                round(record["price"], 2), record["available_quantity"],
            )
            if fingerprint in fingerprints:
                duplicate_candidates += 1
            fingerprints.add(fingerprint)
            if apply:
                product_info_collection.insert_one(record)
            inserted += 1
    return inserted, skipped, invalid, duplicate_candidates, canonical_updates


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write additive product migrations")
    args = parser.parse_args()
    counts = migrate(args.apply)
    print(("Applied" if args.apply else "Dry run") + f": {counts[0]} eligible, {counts[1]} already migrated, {counts[2]} invalid records, {counts[3]} duplicate candidates retained, {counts[4]} canonical field updates planned/applied.")
