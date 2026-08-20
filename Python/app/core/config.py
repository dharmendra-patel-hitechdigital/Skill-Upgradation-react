"""Application configuration.

All runtime knobs live here and are populated from environment variables (or a
local ``.env`` file). Nothing else in the codebase reads ``os.environ`` directly
- that keeps configuration auditable and makes the app trivial to reconfigure
per environment (local / docker / staging / production) without code changes.
"""
from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repository root: .../Python  (this file is app/core/config.py)
BASE_DIR = Path(__file__).resolve().parents[2]

DEFAULT_SECRET_KEY = "CHANGE_ME_use_a_long_random_value"


class Environment(StrEnum):
    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


def _split_csv(value: Any) -> Any:
    """Accept either a JSON array or a plain comma-separated string from env."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):  # JSON array - let pydantic handle it
        import json

        return json.loads(text)
    return [item.strip() for item in text.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ------------------------------------------------------------------ meta
    PROJECT_NAME: str = "Intelligent Document Service"
    VERSION: str = "2.0.0"
    ENVIRONMENT: Environment = Environment.LOCAL
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # -------------------------------------------------------------- database
    # Driver-agnostic URL. The app derives an async driver for runtime and a
    # sync driver for Alembic, so you can write the familiar sync form here.
    #   sqlite:///./app.db
    #   mysql+pymysql://user:pass@localhost:3306/appdb
    DATABASE_URL: str = f"sqlite:///{(BASE_DIR / 'app.db').as_posix()}"

    # Discrete connection parts. When DB_HOST is set these are assembled into
    # DATABASE_URL, overriding it. This exists for deployed environments where
    # the password arrives on its own - an ECS task definition injects a single
    # Secrets Manager JSON key as DB_PASSWORD, and cannot splice it into a URL.
    # Assembling here also percent-encodes the credentials for us, which removes
    # the most common cause of a "can't connect" that is really a parse error.
    DB_HOST: str | None = None
    DB_PORT: int = 3306
    DB_NAME: str | None = None
    DB_USER: str | None = None
    DB_PASSWORD: str | None = None
    DB_DIALECT: str = "mysql"

    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800
    # Seconds to wait for a new TCP connection + handshake. The async MySQL
    # driver's own default is too aggressive for a cross-region managed database
    # (e.g. an RDS instance in eu-north-1 reached from Asia), where the TLS
    # handshake alone can exceed it and surface as a bare "Can't connect" 2003.
    DB_CONNECT_TIMEOUT_SECONDS: int = 30
    # Create tables from the models on startup. Convenient locally, wrong in
    # production (it cannot ALTER, so it drifts). None -> on unless production.
    AUTO_CREATE_TABLES: bool | None = None

    # -------------------------------------------------------------- security
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ISSUER: str = "intelligent-document-service"
    JWT_AUDIENCE: str = "intelligent-document-service.api"
    PASSWORD_MIN_LENGTH: int = 10

    # First user bootstrapped on startup when both values are provided.
    FIRST_ADMIN_EMAIL: str | None = None
    FIRST_ADMIN_PASSWORD: str | None = None

    # ------------------------------------------------------------------ cors
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # --------------------------------------------------------------- uploads
    MAX_UPLOAD_SIZE_MB: int = 20
    ALLOWED_UPLOAD_TYPES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/tiff",
            "text/plain",
        ]
    )

    # --------------------------------------------------------------- storage
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    STORAGE_LOCAL_DIR: Path = BASE_DIR / "var" / "documents"
    S3_BUCKET: str | None = None
    S3_PREFIX: str = "documents"

    # ------------------------------------------------------------------- aws
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None

    # ------------------------------------------------------- ai: text layer
    # auto  -> Textract when configured, else the local PDF/plain-text reader
    # textract / local / none  -> force a specific engine
    OCR_PROVIDER: Literal["auto", "textract", "local", "none"] = "auto"
    TEXTRACT_ENABLED: bool = False
    TEXTRACT_MAX_SYNC_BYTES: int = 5 * 1024 * 1024  # AWS sync-API hard limit

    # ---------------------------------------------------- ai: analysis layer
    # auto -> OpenAI when an API key is present, else the heuristic analyzer
    LLM_PROVIDER: Literal["auto", "openai", "heuristic", "none"] = "auto"
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: str | None = None
    OPENAI_TIMEOUT_SECONDS: float = 60.0
    OPENAI_MAX_RETRIES: int = 2
    # Set to an empty value to omit `temperature` entirely - reasoning models
    # reject any value other than their default.
    OPENAI_TEMPERATURE: float | None = 0.0
    # Guard-rail so a 300-page scan can never blow up the prompt or the bill.
    LLM_MAX_INPUT_CHARS: int = 24_000

    # ------------------------------------------------------------ processing
    # Documents are processed out-of-band; this caps concurrent AI pipelines.
    PROCESSING_MAX_CONCURRENCY: int = 4
    PROCESSING_TIMEOUT_SECONDS: float = 180.0

    # ------------------------------------------------------------- observability
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    # ---------------------------------------------------------------- validators
    @field_validator("CORS_ORIGINS", "ALLOWED_UPLOAD_TYPES", mode="before")
    @classmethod
    def _parse_list(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _assemble_database_url(self) -> Settings:
        """Build DATABASE_URL from the discrete DB_* parts when DB_HOST is set.

        Runs before ``_guard_production_defaults`` (model validators fire in
        definition order), so the production guards below see the final URL.

        Credentials are percent-encoded here rather than by whoever wrote the
        secret: a generated RDS password routinely contains ``/``, ``@`` or
        ``%``, each of which silently changes how the URL parses.
        """
        if not self.DB_HOST:
            return self

        from urllib.parse import quote_plus

        user = quote_plus(self.DB_USER or "")
        auth = f"{user}:{quote_plus(self.DB_PASSWORD)}@" if self.DB_PASSWORD else f"{user}@"
        self.DATABASE_URL = (
            f"{self.DB_DIALECT}://{auth}{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME or ''}"
        )
        return self

    @model_validator(mode="after")
    def _guard_production_defaults(self) -> Settings:
        if self.is_production:
            if self.SECRET_KEY == DEFAULT_SECRET_KEY:
                raise ValueError(
                    "SECRET_KEY must be overridden in production. Generate one with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
                )
            if self.DEBUG:
                raise ValueError("DEBUG must be False in production.")
            if "*" in self.CORS_ORIGINS:
                raise ValueError("CORS_ORIGINS must not be '*' in production.")
        if self.STORAGE_BACKEND == "s3" and not self.S3_BUCKET:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND='s3'.")
        return self

    # ---------------------------------------------------------------- helpers
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT is Environment.PRODUCTION

    @property
    def is_testing(self) -> bool:
        return self.ENVIRONMENT is Environment.TEST

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @property
    def should_auto_create_tables(self) -> bool:
        """Explicit setting wins; otherwise on everywhere except production."""
        if self.AUTO_CREATE_TABLES is not None:
            return self.AUTO_CREATE_TABLES
        return not self.is_production

    @computed_field  # type: ignore[prop-decorator]
    @property
    def async_database_url(self) -> str:
        """DATABASE_URL rewritten onto an asyncio-capable driver."""
        return _rewrite_driver(self.DATABASE_URL, async_mode=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """DATABASE_URL rewritten onto a blocking driver (used by Alembic)."""
        return _rewrite_driver(self.DATABASE_URL, async_mode=False)

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


# Map of `dialect` -> (async driver, sync driver)
_DRIVERS: dict[str, tuple[str, str]] = {
    "sqlite": ("aiosqlite", "pysqlite"),
    "mysql": ("aiomysql", "pymysql"),
    "mariadb": ("aiomysql", "pymysql"),
    "postgresql": ("asyncpg", "psycopg2"),
}


def _rewrite_driver(url: str, *, async_mode: bool) -> str:
    """Swap the DBAPI driver in a SQLAlchemy URL for its async/sync sibling.

    Uses SQLAlchemy's own URL parser rather than ``urllib`` because a
    ``sqlite:///relative/path.db`` URL has an empty netloc, and
    ``urlunsplit`` silently collapses ``:///`` to ``:/`` in that case.
    Credentials, host, database and query string are preserved, so one
    ``DATABASE_URL`` can serve both the app and Alembic.
    """
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    drivers = _DRIVERS.get(parsed.get_backend_name())
    if drivers is None:  # unknown dialect - trust the operator's URL verbatim
        return url
    driver = drivers[0] if async_mode else drivers[1]
    return parsed.set(drivername=f"{parsed.get_backend_name()}+{driver}").render_as_string(
        hide_password=False
    )


@lru_cache
def get_settings() -> Settings:
    """Cached accessor - settings are parsed once per process.

    Exposed as a FastAPI dependency so tests can override it via
    ``app.dependency_overrides``.
    """
    return Settings()


def generate_secret_key() -> str:
    """Convenience helper used by the CLI snippets in the docs."""
    return secrets.token_urlsafe(48)


settings = get_settings()
