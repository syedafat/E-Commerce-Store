"""Central application and MongoDB configuration."""
import os
from datetime import timedelta
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()


def env_bool(name, default):
    value = os.environ.get(name, default).lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise RuntimeError(f"{name} must be set to true or false.")
    return value in {"true", "1", "yes"}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    MONGO_URI = os.environ.get("MONGO_URI")
    MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME")
    APP_ENV = os.environ.get("APP_ENV", "development").lower()
    MONGO_REQUIRE_TRANSACTIONS = env_bool(
        "MONGO_REQUIRE_TRANSACTIONS", "true" if APP_ENV == "production" else "false"
    )
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", "false")
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024

    @classmethod
    def validate(cls):
        if cls.APP_ENV not in {"development", "test", "staging", "production"}:
            raise RuntimeError("APP_ENV must be development, test, staging, or production.")
        secret = cls.SECRET_KEY
        if not secret or len(secret.encode("utf-8")) < 32 or len(set(secret)) < 12 or "replace-with" in secret.lower():
            raise RuntimeError("SECRET_KEY is required and must be a non-placeholder key of at least 32 bytes.")
        uri = cls.MONGO_URI or ""
        parsed = None
        try:
            parsed = urlsplit(uri)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            host = None
            port = None
        if parsed is None or parsed.scheme not in {"mongodb", "mongodb+srv"} or not host or re.search(r"\s", uri):
            raise RuntimeError("MONGO_URI is required and must be a valid mongodb:// or mongodb+srv:// URI.")
        if parsed.scheme == "mongodb+srv" and port is not None:
            raise RuntimeError("MONGO_URI is required and must be a valid mongodb:// or mongodb+srv:// URI.")
        db_name = cls.MONGO_DB_NAME or ""
        if not db_name or len(db_name) > 63 or re.search(r'[\\/. "$*<>:|?]', db_name):
            raise RuntimeError("MONGO_DB_NAME is required and must be a valid MongoDB database name.")
        if cls.APP_ENV == "production":
            if not cls.MONGO_REQUIRE_TRANSACTIONS:
                raise RuntimeError("MONGO_REQUIRE_TRANSACTIONS=true is required in production.")
            if not cls.SESSION_COOKIE_SECURE:
                raise RuntimeError("SESSION_COOKIE_SECURE=true is required in production.")
            if not cls.RATELIMIT_STORAGE_URI.startswith(("redis://", "rediss://")):
                raise RuntimeError("Configure shared RATELIMIT_STORAGE_URI (for example Redis) in production.")
