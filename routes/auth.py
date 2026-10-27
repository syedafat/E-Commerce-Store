from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

from models import admin_collection, user_collection
from extensions import limiter

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or len(name) > 100 or "@" not in email or len(email) > 254:
            flash("Enter a valid name and email address.", "error")
        elif len(password) < 12:
            flash("Password must be at least 12 characters.", "error")
        else:
            try:
                user_collection.insert_one({
                    "name": name, "email": email,
                    "password_hash": generate_password_hash(password), "role": "user"
                })
                flash("Account created. You can now sign in.", "success")
                return redirect(url_for("auth.login"))
            except DuplicateKeyError:
                flash("An account with that email already exists.", "error")
            except PyMongoError:
                flash("Registration is temporarily unavailable. Please try again later.", "error")
    return render_template("register.html")


@auth_bp.route("/", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        try:
            matching_users = list(user_collection.find({"email": email}).limit(2))
        except PyMongoError:
            return render_template("error.html", message="Sign in is temporarily unavailable."), 503
        if len(matching_users) > 1:
            current_app.logger.error("Login blocked because multiple user records share an email address.")
            flash("This account needs administrator review before sign-in. Contact support.", "error")
            return render_template("login.html")
        user = matching_users[0] if matching_users else None
        valid = False
        if user:
            stored_hash = user.get("password_hash")
            if stored_hash:
                try:
                    valid = check_password_hash(stored_hash, password)
                except (ValueError, TypeError):
                    valid = False
            # One-time migration for existing plaintext accounts: upgrade on successful login.
            elif user.get("password") and user["password"] == password:
                stored_hash = generate_password_hash(password)
                try:
                    upgraded = user_collection.update_one(
                        {"_id": user["_id"], "password": password},
                        {"$set": {"password_hash": stored_hash}, "$unset": {"password": ""}}
                    )
                    valid = upgraded.modified_count == 1
                    if not valid:
                        latest = user_collection.find_one({"_id": user["_id"]})
                        valid = bool(latest and latest.get("password_hash") and
                                     check_password_hash(latest["password_hash"], password))
                except PyMongoError:
                    return render_template("error.html", message="Sign in is temporarily unavailable."), 503
                except (ValueError, TypeError):
                    valid = False
        if not valid:
            flash("Email or password is incorrect.", "error")
        else:
            role = user.get("role", "user")
            if role == "admin":
                try:
                    authorized = admin_collection.find_one({"user_id": user["_id"]})
                except PyMongoError:
                    return render_template("error.html", message="Sign in is temporarily unavailable."), 503
                if not authorized:
                    role = "user"
            session.clear()
            session["user_id"] = str(user["_id"])
            session["user"] = user.get("name", "")
            session["role"] = role
            from authz import csrf_token
            csrf_token()
            return redirect(url_for("admin_dashboard.admin_dashboard") if role == "admin" else url_for("user_home"))
    return render_template("login.html")


@auth_bp.post("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))
