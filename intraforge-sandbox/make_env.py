"""Write intraforge-sandbox/.env with freshly generated secrets (never committed). Refuses to overwrite an existing one:
the sandbox's databases were initialised with those passwords and would no longer open."""

import secrets
import sys
from pathlib import Path

target = Path(__file__).with_name(".env")
if target.exists():
    sys.exit(f"{target} already exists; delete the sandbox volumes before generating a new one.")

db = secrets.token_urlsafe(24)
store = secrets.token_urlsafe(24)
values = {
    "DJANGO_SETTINGS_MODULE": "config.settings.prod",
    "SECRET_KEY": secrets.token_urlsafe(48),
    "CREDENTIAL_ENCRYPTION_KEY": secrets.token_urlsafe(48),
    "BACKUP_ENCRYPTION_KEY": secrets.token_urlsafe(32),
    "DEBUG": "False",
    "ALLOWED_HOSTS": "localhost,127.0.0.1,backend",
    "CSRF_TRUSTED_ORIGINS": "https://localhost:9443",
    "OIDC_ISSUER": "https://localhost:9443",
    "POSTGRES_USER": "intraforge",
    "POSTGRES_DB": "intraforge",
    "POSTGRES_PASSWORD": db,
    "CONTROL_DB_HOST": "postgres-control",
    "CONTROL_DB_NAME": "intraforge",
    "CONTROL_DB_USER": "intraforge",
    "CONTROL_DB_PASSWORD": db,
    "TENANT_DB_HOST": "postgres-tenant",
    "TENANT_DB_NAME": "intraforge",
    "TENANT_DB_USER": "intraforge",
    "TENANT_DB_PASSWORD": db,
    "OBJECT_STORAGE_ENDPOINT": "http://object-storage:9000",
    "OBJECT_STORAGE_BUCKET_PREFIX": "intraforge",
    "OBJECT_STORAGE_ROOT_USER": "intraforge",
    "OBJECT_STORAGE_ROOT_PASSWORD": store,
    "MINIO_ROOT_USER": "intraforge",
    "MINIO_ROOT_PASSWORD": store,
    "REDIS_URL": "redis://valkey:6379/0",
    "CACHE_URL": "redis://valkey:6379/1",
}
target.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
print(f"wrote {target}")
