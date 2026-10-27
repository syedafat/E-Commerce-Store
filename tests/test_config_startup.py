import os
import subprocess
import sys


def _check_config(**updates):
    env = os.environ.copy()
    for key in ("SECRET_KEY", "MONGO_URI", "MONGO_DB_NAME", "APP_ENV", "SESSION_COOKIE_SECURE", "RATELIMIT_STORAGE_URI", "MONGO_REQUIRE_TRANSACTIONS"):
        env.pop(key, None)
    env.update({
        "SECRET_KEY": "Abcdef0123456789" * 4,
        "MONGO_URI": "mongodb://127.0.0.1:27017/ecommerce_test",
        "MONGO_DB_NAME": "ecommerce_test",
        **updates,
    })
    # Do not let a developer's workspace .env leak into subprocess configuration tests.
    env["PYTHON_DOTENV_DISABLED"] = "true"
    return subprocess.run(
        [sys.executable, "-c", "from models import client"],
        cwd=os.path.dirname(os.path.dirname(__file__)), env=env,
        text=True, capture_output=True, timeout=15,
    )


def test_configuration_rejects_missing_secret_without_creating_client():
    result = _check_config(SECRET_KEY="")
    assert result.returncode != 0
    assert "SECRET_KEY is required" in result.stderr


def test_configuration_rejects_repeated_or_placeholder_secret():
    for secret in ("s" * 64, "replace-with-a-random-64-character-hex-string"):
        result = _check_config(SECRET_KEY=secret)
        assert result.returncode != 0
        assert "SECRET_KEY is required" in result.stderr


def test_configuration_rejects_invalid_or_missing_mongo_uri():
    for uri in ("", "not-a-mongodb-uri", "mongodb://", "mongodb://:27017/ecommerce", "mongodb://[::1"):
        result = _check_config(MONGO_URI=uri)
        assert result.returncode != 0
        assert "MONGO_URI is required" in result.stderr


def test_configuration_rejects_missing_or_invalid_database_name():
    for database in ("", "invalid/name", "has space"):
        result = _check_config(MONGO_DB_NAME=database)
        assert result.returncode != 0
        assert "MONGO_DB_NAME is required" in result.stderr


def test_configuration_rejects_unknown_environment_and_bad_booleans():
    env = _check_config(APP_ENV="prod")
    assert env.returncode != 0
    assert "APP_ENV must be" in env.stderr
    boolean = _check_config(SESSION_COOKIE_SECURE="sometimes")
    assert boolean.returncode != 0
    assert "SESSION_COOKIE_SECURE must" in boolean.stderr


def test_production_rejects_insecure_cookie_and_local_rate_limit_store():
    result = _check_config(APP_ENV="production", SESSION_COOKIE_SECURE="false", RATELIMIT_STORAGE_URI="memory://")
    assert result.returncode != 0
    assert "SESSION_COOKIE_SECURE=true" in result.stderr


def test_production_rejects_memory_limiter_after_secure_cookie_enabled():
    result = _check_config(APP_ENV="production", SESSION_COOKIE_SECURE="true", RATELIMIT_STORAGE_URI="memory://")
    assert result.returncode != 0
    assert "shared RATELIMIT_STORAGE_URI" in result.stderr


def test_production_rejects_missing_transaction_requirement():
    result = _check_config(APP_ENV="production", SESSION_COOKIE_SECURE="true",
                           RATELIMIT_STORAGE_URI="redis://localhost", MONGO_REQUIRE_TRANSACTIONS="false")
    assert result.returncode != 0
    assert "MONGO_REQUIRE_TRANSACTIONS=true" in result.stderr


def test_valid_local_config_imports_without_connecting_to_mongo():
    result = _check_config()
    assert result.returncode == 0, result.stderr


def test_valid_production_config_passes_startup_validation_without_db_access():
    result = _check_config(APP_ENV="production", SESSION_COOKIE_SECURE="true",
                           RATELIMIT_STORAGE_URI="rediss://localhost:6379/0",
                           MONGO_REQUIRE_TRANSACTIONS="true")
    assert result.returncode == 0, result.stderr
