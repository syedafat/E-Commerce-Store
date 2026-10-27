import click
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, session
from pymongo.errors import PyMongoError
from werkzeug.security import generate_password_hash

from authz import csrf_token, protect_csrf
from config import Config
from extensions import limiter
from models import admin_collection, database_is_available, initialize_indexes, user_collection
from routes.auth import auth_bp
from routes.admin_dashboard import admin_bp
from routes.product_management import product_bp
from routes.product_information import product_information_bp
from routes.inventory import inventory_bp
from routes.inventory_details import inventory_details_bp
from routes.sales_management import sales_bp
from routes.realtime_prediction import prediction_bp
from routes.reports import reports_bp


def _duplicate_email_groups():
    return list(user_collection.aggregate([
        {"$match": {"email": {"$type": "string"}}},
        {"$group": {"_id": "$email", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"_id": 1}},
    ]))


def _ensure_unique_user_emails():
    try:
        duplicates = _duplicate_email_groups()
    except PyMongoError as exc:
        raise click.ClickException("Could not check user emails before creating indexes.") from exc
    if duplicates:
        raise click.ClickException(
            "Duplicate user emails prevent safe index creation. Run `flask --app app check-duplicate-emails`, "
            "review and reconcile the records, then retry. No user records were changed."
        )


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("Set SECRET_KEY in the environment or .env before starting the app.")

    for blueprint in (auth_bp, admin_bp, product_bp, product_information_bp,
                      inventory_bp, inventory_details_bp, sales_bp, prediction_bp, reports_bp):
        app.register_blueprint(blueprint)
    limiter.init_app(app)
    protect_csrf(app)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def make_session_permanent():
        session.permanent = True

    @app.errorhandler(503)
    def database_unavailable(_error):
        return render_template("error.html", message="The database is temporarily unavailable."), 503

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    def create_admin(email, name):
        """Create an administrator from a trusted terminal, never public signup."""
        email = email.strip().lower()
        if not database_is_available():
            raise click.ClickException("MongoDB is unavailable; check MONGO_URI.")
        _ensure_unique_user_emails()
        if user_collection.find_one({"email": email}):
            raise click.ClickException("An account with this email already exists. Review it, then use grant-admin if appropriate.")
        password = click.prompt("Administrator password", hide_input=True, confirmation_prompt=True)
        if not email or not name.strip() or len(password) < 12:
            raise click.ClickException("Provide a name, email, and password of at least 12 characters.")
        try:
            user_collection.create_index("email", unique=True, name="users_email_unique")
            admin_collection.create_index("user_id", unique=True, name="admin_user_unique")
            created = user_collection.insert_one({
                "name": name.strip(), "email": email,
                "password_hash": generate_password_hash(password), "role": "admin"
            })
            admin_collection.insert_one({
                "user_id": created.inserted_id, "email": email,
                "created_at": datetime.now(timezone.utc),
            })
        except Exception as exc:
            # Avoid printing database exception text, which may contain connection details.
            raise click.ClickException("Could not create administrator. Check whether the email already exists.") from exc
        click.echo("Administrator created.")

    @app.cli.command("grant-admin")
    @click.option("--email", prompt=True)
    def grant_admin(email):
        """Authorize an existing account as admin after operator identity review."""
        email = email.strip().lower()
        if not email or not database_is_available():
            raise click.ClickException("MongoDB is unavailable or the email is invalid.")
        _ensure_unique_user_emails()
        try:
            admin_collection.create_index("user_id", unique=True, name="admin_user_unique")
            user_collection.create_index("email", unique=True, name="users_email_unique")
            matching_users = list(user_collection.find({"email": email}).limit(2))
            if not matching_users:
                raise click.ClickException("No account exists with that email.")
            if len(matching_users) != 1:
                raise click.ClickException("Multiple accounts use this email. Resolve the duplicate records before granting admin access.")
            user = matching_users[0]
            click.confirm(f"Grant administrator access to {email}?", abort=True)
            user_collection.update_one({"_id": user["_id"]}, {"$set": {"role": "admin"}})
            admin_collection.update_one(
                {"user_id": user["_id"]},
                {"$setOnInsert": {
                    "user_id": user["_id"], "email": email,
                    "created_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException("Could not grant administrator access. Check duplicate records and indexes.") from exc
        click.echo("Administrator access granted.")

    @app.cli.command("init-db")
    def init_db():
        """Check MongoDB and create non-destructive indexes."""
        if not database_is_available():
            raise click.ClickException("MongoDB is unavailable; check MONGO_URI.")
        _ensure_unique_user_emails()
        initialize_indexes()
        click.echo("MongoDB connection verified and indexes are ready.")

    @app.cli.command("check-duplicate-emails")
    def check_duplicate_emails():
        """Read-only report of email values that block the required unique index."""
        if not database_is_available():
            raise click.ClickException("MongoDB is unavailable; check MONGO_URI.")
        try:
            groups = list(user_collection.aggregate([
                {"$match": {"email": {"$type": "string"}}},
                {"$group": {"_id": "$email", "count": {"$sum": 1}}},
                {"$match": {"count": {"$gt": 1}}},
                {"$sort": {"_id": 1}},
            ]))
        except PyMongoError as exc:
            raise click.ClickException("Could not inspect duplicate user emails.") from exc
        if not groups:
            click.echo("No duplicate email values found.")
            return
        click.echo("Duplicate email values (records are unchanged):")
        for group in groups:
            click.echo(f"- {group['_id']}: {group['count']} records")
        click.echo("Review and reconcile these accounts manually, then rerun init-db.")

    @app.cli.command("inspect-user-email")
    @click.option("--email", prompt=True)
    def inspect_user_email(email):
        """Read-only account overview, excluding all password fields."""
        email = email.strip().lower()
        if not email or not database_is_available():
            raise click.ClickException("MongoDB is unavailable or the email is invalid.")
        try:
            accounts = list(user_collection.find(
                {"email": email}, {"name": 1, "email": 1, "role": 1, "created_at": 1}
            ).limit(20))
            admin_ids = {str(row["user_id"]) for row in admin_collection.find(
                {"user_id": {"$in": [account["_id"] for account in accounts]}}, {"user_id": 1}
            )}
        except PyMongoError as exc:
            raise click.ClickException("Could not inspect matching user accounts.") from exc
        if not accounts:
            click.echo("No account found for that email.")
            return
        click.echo(f"{len(accounts)} account record(s) found:")
        for account in accounts:
            click.echo(f"- id={account['_id']} name={account.get('name', '(missing)')} "
                       f"role={account.get('role', 'user')} admin_grant={str(account['_id']) in admin_ids}")
        click.echo("This report omits password hashes. It makes no database changes.")

    @app.cli.command("reset-user-password")
    @click.option("--email", prompt=True)
    def reset_user_password(email):
        """Reset a uniquely matched account password from a trusted terminal."""
        email = email.strip().lower()
        if not email or not database_is_available():
            raise click.ClickException("MongoDB is unavailable or the email is invalid.")
        try:
            matches = list(user_collection.find({"email": email}).limit(2))
            if len(matches) != 1:
                raise click.ClickException("Expected exactly one account for that email; inspect and resolve duplicates first.")
            click.confirm(f"Reset the password for account {email}?", abort=True)
            password = click.prompt("New password (at least 12 characters)", hide_input=True, confirmation_prompt=True)
            if len(password) < 12:
                raise click.ClickException("Password must be at least 12 characters.")
            user_collection.update_one({"_id": matches[0]["_id"]},
                                       {"$set": {"password_hash": generate_password_hash(password)},
                                        "$unset": {"password": ""}})
        except click.ClickException:
            raise
        except PyMongoError as exc:
            raise click.ClickException("Could not update the account password.") from exc
        click.echo("Password updated.")

    @app.get("/user")
    def user_home():
        if not session.get("user_id"):
            from flask import redirect, url_for
            return redirect(url_for("auth.login"))
        return render_template("user_dashboard.html", name=session.get("user", ""))

    @app.get("/data-storage")
    def legacy_data_storage():
        from flask import redirect, url_for
        return redirect(url_for("inventory_details.view_all_inventory_details"))

    @app.get("/realtime")
    def legacy_realtime():
        from flask import redirect, url_for
        return redirect(url_for("prediction.predict_pnl"))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=app.config.get("DEBUG", False))
