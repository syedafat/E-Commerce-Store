"""Backward-compatible import for the sole admin dashboard blueprint."""
from routes.admin_dashboard import admin_bp

__all__ = ["admin_bp"]
