"""Shared authorization and request protection helpers."""
from functools import wraps
import secrets

from flask import abort, redirect, request, session, url_for
from bson import ObjectId
from pymongo.errors import PyMongoError

from models import admin_collection, user_collection


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def protect_csrf(app):
    @app.before_request
    def validate_csrf():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token")
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                abort(400, description="The form expired. Reload the page and try again.")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id or not ObjectId.is_valid(user_id):
            session.clear()
            return redirect(url_for("auth.login"))
        try:
            user = user_collection.find_one({"_id": ObjectId(user_id)}, {"role": 1})
            grant = admin_collection.find_one({"user_id": ObjectId(user_id)}, {"_id": 1})
        except PyMongoError:
            abort(503)
        if not user:
            session.clear()
            return redirect(url_for("auth.login"))
        session["role"] = user.get("role", "user")
        if session["role"] != "admin" or not grant:
            abort(403)
        return view(*args, **kwargs)
    return wrapped
