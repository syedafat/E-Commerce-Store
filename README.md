# E-Commerce Admin

A Flask/Jinja administration app with MongoDB-backed users, products, inventory, sales, reports, and a transparent historical baseline forecast.

The administration UI shares a responsive navigation shell and common form, card, and table styles. The overview supports a date range; product and sales lists are searchable and paginated; inventory adjustments retain a bounded per-product audit history; sales details preserve the recorded transaction values; and CSV, Excel, and PDF reports use the selected dates for actual sales data. Product removal is a soft archive so sales and stock history remain available.

## Prerequisites

- Python 3.10 or newer
- MongoDB local server or a MongoDB Atlas database

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`. Set `MONGO_URI` and `MONGO_DB_NAME` for the intended database; startup now fails on missing or malformed values. Generate the Flask key with:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste that output as `SECRET_KEY`. Use `SESSION_COOKIE_SECURE=true` when serving over HTTPS. Never commit `.env` or use the example key in a deployed environment.

## MongoDB and legacy data

The canonical product collection is `product_information`. Sales and inventory now use its `product_name`, `price`, `available_quantity`, `category`, `description`, `created_at`, and `updated_at` fields. The app connects through one shared `MongoClient`. Administrator authorization requires both the user role and an operator-created record in `admin_accounts`; legacy role fields alone do not grant access.

Initialize indexes after MongoDB is reachable:

```powershell
flask --app app init-db
```

If the user collection contains duplicate email addresses, `init-db`, `create-admin`, and `grant-admin` stop before changing records. Use the read-only commands below, review the matching accounts in MongoDB/Compass, back up first, and decide manually which account records to retain or reconcile. The application does not delete or merge user accounts automatically. Login is blocked for an email that matches multiple user records until it is resolved.

```powershell
flask --app app check-duplicate-emails
flask --app app inspect-user-email --email reviewed-account@example.com
```

`inspect-user-email` prints account IDs, names, roles, and whether each has an admin grant. It never prints password hashes.

Product migration is additive and preserves both source collections. Start with a read-only preview, then review the counts and apply:

```powershell
python scripts/migrate_products.py
python scripts/migrate_products.py --apply
```

It imports legacy `products` rows into `product_information` under an idempotent migration key. The dry run reports candidate duplicate matches and planned canonical-field updates; duplicate candidates are retained as separate records so the script does not silently collapse potentially distinct stock. Existing canonical values are preserved except parseable string values for `price` and `available_quantity`, which are converted to numeric types. Back up the database before applying any migration. The migration does not delete records.

## First administrator and existing passwords

Create the initial administrator from a trusted terminal (the public registration form always creates a regular user; the password is entered through a hidden prompt):

```powershell
flask --app app create-admin
```

The app also stores a separate CLI-managed admin grant. Legacy accounts with `role=admin` from the old public registration flow are not authorized until an operator reviews and grants them explicitly:

```powershell
flask --app app grant-admin --email reviewed-account@example.com
```

New passwords are stored as Werkzeug hashes. Existing plaintext-password accounts are upgraded to hashes after a successful login; remove any backups or exports that contain those legacy plaintext values.

The MongoDB credential that was previously embedded in the source has been removed. **Rotate that database user’s password in MongoDB Atlas**, update `MONGO_URI` in your local `.env`, and review Atlas access logs and network access rules. Source cleanup cannot revoke a credential that may already have been copied.

## Run

```powershell
flask --app app run
```

For production, serve with a production WSGI server behind HTTPS. Do not use Flask debug mode in production.

## Production deployment checklist

1. Use a dedicated MongoDB replica set or sharded cluster on a supported MongoDB release, plus a shared Redis service. Use a dedicated database user with only the required database permissions.
2. Set deployment environment variables: `APP_ENV=production`, `SECRET_KEY` (fresh random 64-character hex), `MONGO_URI`, `MONGO_DB_NAME`, `MONGO_REQUIRE_TRANSACTIONS=true`, `SESSION_COOKIE_SECURE=true`, and `RATELIMIT_STORAGE_URI=rediss://...`.
3. Install the pinned dependency ranges with `python -m pip install -r requirements.txt`; terminate HTTPS at a trusted reverse proxy and restrict inbound access to the WSGI service.
4. Run `flask --app app init-db`; resolve any duplicate email or admin grant records reported by MongoDB before proceeding.
5. Back up the intended database. Run `python scripts/migrate_products.py` and review eligible, invalid, duplicate-candidate, and canonical-update counts. Apply with `--apply` only after review and authorization.
6. Create the first administrator with `flask --app app create-admin`. Review legacy admin-role accounts and explicitly authorize any legitimate ones with the confirmed `grant-admin` CLI command.
7. Run `python -m pytest -q` in the deployment build environment, then serve with `waitress-serve --call app:create_app --host=127.0.0.1 --port=8000` behind the HTTPS proxy.

Do not point migration dry runs or tests at production unless the configured database is explicitly intended and the read-only effect is accepted. The automated test suite uses an isolated mock database.

## Tests

Tests use `mongomock`; they do not connect to or modify the configured MongoDB database:

```powershell
python -m pytest -q
```

## Security and operational notes

- All state-changing forms require a session-bound CSRF token.
- Product rows with sales history cannot be removed; other products are archived instead of physically deleted. Archived items are excluded from new sales and active inventory totals.
- Manual stock adjustments use an expected-quantity guard to prevent stale forms overwriting concurrent edits. Each change and its latest 100 audit entries are written atomically to the product record; an indexed movement copy supports history views.
- Dashboard sales and stock totals stream database cursors. The forecast uses at most the latest 5,000 dated sale records available before the selected cutoff, to keep dashboard work bounded; the full forecast page reports its own data cutoff and limitations.
- The dashboard and sales trend use actual sale dates. Daily, weekly, and monthly exports group recorded sales; the forecast export is separate from actuals.
- Administrator routes require an authenticated administrator session.
- Login and registration POSTs have per-IP rate limits. The default in-memory limiter is suitable for local use; configure `RATELIMIT_STORAGE_URI` to a shared Redis backend for multi-worker production deployments.
- Stock decrements and sale insertion run in one transaction when MongoDB supports transactions.
- In `APP_ENV=production`, the app requires `MONGO_REQUIRE_TRANSACTIONS=true`, secure session cookies, and shared rate-limit storage. Sales use MongoDB transactions when `hello` reports a replica set or `mongos`; configure MongoDB as a replica set (Atlas clusters already provide this) or sharded cluster. Local standalone MongoDB can use the compensating fallback only outside production.
- If a standalone fallback cannot determine whether the sale insert succeeded or cannot restore stock, it writes a `sales_reconciliation` record when possible and logs a critical operation ID. Reconcile these records against `sales_records` and product stock before resuming sales. This fallback cannot guarantee crash-proof consistency.
- Forecasts use the most recent four weekly totals, require at least eight dated weeks, and report rolling one-week-ahead mean absolute error. This baseline does not claim validated accuracy and may be unsuitable when sales are sparse or seasonal.
- No tax rate is configured, so recorded tax is zero and sale total is subtotal less discount. Configure a jurisdiction-appropriate tax rule before relying on tax reporting.
- If an existing user collection contains duplicate email addresses, resolve those duplicates before `init-db` can create its unique email index.
