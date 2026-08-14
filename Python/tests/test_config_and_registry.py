"""Configuration parsing, production safety rails, and provider selection."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_SECRET_KEY, Environment, Settings, _rewrite_driver
from app.core.exceptions import AIProviderUnavailableError, ExtractionError


def make_settings(**overrides: object) -> Settings:
    """Build settings without inheriting the developer's .env or environment."""
    base: dict[str, object] = {
        "ENVIRONMENT": "local",
        "SECRET_KEY": "a-long-enough-test-secret-key-value-here",
        "DATABASE_URL": "sqlite:///./test.db",
        "_env_file": None,  # ignore .env so the test is hermetic
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


# ------------------------------------------------------------------ url rewriting
@pytest.mark.parametrize(
    ("url", "expected_async", "expected_sync"),
    [
        ("sqlite:///./app.db", "sqlite+aiosqlite:///./app.db", "sqlite+pysqlite:///./app.db"),
        (
            "mysql+pymysql://root:pw@localhost:3306/appdb",
            "mysql+aiomysql://root:pw@localhost:3306/appdb",
            "mysql+pymysql://root:pw@localhost:3306/appdb",
        ),
        (
            "postgresql://u:p@db:5432/appdb",
            "postgresql+asyncpg://u:p@db:5432/appdb",
            "postgresql+psycopg2://u:p@db:5432/appdb",
        ),
    ],
)
def test_one_database_url_serves_both_the_app_and_alembic(
    url: str, expected_async: str, expected_sync: str
) -> None:
    assert _rewrite_driver(url, async_mode=True) == expected_async
    assert _rewrite_driver(url, async_mode=False) == expected_sync


def test_relative_sqlite_paths_keep_their_triple_slash() -> None:
    """urlunsplit collapses ':///' to ':/' on an empty netloc - a real trap."""
    assert _rewrite_driver("sqlite:///./app.db", async_mode=True).count("/") >= 3


def test_special_characters_in_a_password_survive_rewriting() -> None:
    url = "mysql+pymysql://root:p%40ss%2Fword@localhost:3306/appdb"
    rewritten = _rewrite_driver(url, async_mode=True)
    assert "p%40ss%2Fword" in rewritten


def test_an_unknown_dialect_is_passed_through_untouched() -> None:
    url = "oracle+cx_oracle://user:pw@host:1521/db"
    assert _rewrite_driver(url, async_mode=True) == url


# ------------------------------------------------------------------- list parsing
def test_list_settings_accept_comma_separated_values() -> None:
    settings = make_settings(CORS_ORIGINS="https://a.example, https://b.example")
    assert settings.CORS_ORIGINS == ["https://a.example", "https://b.example"]


def test_list_settings_accept_json_arrays() -> None:
    settings = make_settings(CORS_ORIGINS='["https://a.example","https://b.example"]')
    assert settings.CORS_ORIGINS == ["https://a.example", "https://b.example"]


def test_empty_list_setting_is_empty_not_a_blank_entry() -> None:
    assert make_settings(CORS_ORIGINS="").CORS_ORIGINS == []


# --------------------------------------------------------- production safety rails
def test_production_refuses_the_default_secret_key() -> None:
    """The single most damaging misconfiguration must fail at startup, loudly."""
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make_settings(ENVIRONMENT="production", SECRET_KEY=DEFAULT_SECRET_KEY)


def test_production_refuses_debug_mode() -> None:
    with pytest.raises(ValidationError, match="DEBUG"):
        make_settings(ENVIRONMENT="production", DEBUG=True)


def test_production_refuses_wildcard_cors() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        make_settings(ENVIRONMENT="production", CORS_ORIGINS="*")


def test_s3_backend_requires_a_bucket() -> None:
    with pytest.raises(ValidationError, match="S3_BUCKET"):
        make_settings(STORAGE_BACKEND="s3", S3_BUCKET=None)


def test_a_valid_production_configuration_is_accepted() -> None:
    settings = make_settings(
        ENVIRONMENT="production",
        SECRET_KEY="a-genuinely-long-random-production-secret-value",
        CORS_ORIGINS="https://app.example.com",
        DEBUG=False,
    )
    assert settings.is_production
    # create_all must be off in production: it cannot express an ALTER.
    assert settings.should_auto_create_tables is False


def test_auto_create_tables_defaults_on_outside_production() -> None:
    assert make_settings(ENVIRONMENT="local").should_auto_create_tables is True
    assert make_settings(ENVIRONMENT="test").should_auto_create_tables is True


def test_auto_create_tables_can_be_forced() -> None:
    settings = make_settings(
        ENVIRONMENT="production",
        SECRET_KEY="a-genuinely-long-random-production-secret-value",
        CORS_ORIGINS="https://app.example.com",
        AUTO_CREATE_TABLES=True,
    )
    assert settings.should_auto_create_tables is True


def test_derived_helpers() -> None:
    settings = make_settings(MAX_UPLOAD_SIZE_MB=7)
    assert settings.max_upload_bytes == 7 * 1024 * 1024
    assert settings.is_sqlite is True
    assert make_settings(ENVIRONMENT="test").is_testing


def test_openai_temperature_can_be_disabled_for_reasoning_models() -> None:
    """Reasoning models reject any explicit temperature, so it must be omittable."""
    assert make_settings(OPENAI_TEMPERATURE=None).OPENAI_TEMPERATURE is None
    assert make_settings(OPENAI_TEMPERATURE=0.2).OPENAI_TEMPERATURE == 0.2


def test_environment_enum_values() -> None:
    assert Environment.PRODUCTION.value == "production"
    assert make_settings(ENVIRONMENT="staging").ENVIRONMENT is Environment.STAGING


# ---------------------------------------------------------- provider selection
def test_extractor_selection_without_textract(monkeypatch) -> None:
    """PDFs and text work offline; images must fail with an actionable message."""
    from app.services.ai import registry

    registry.reset_provider_cache()

    assert registry.get_text_extractor("application/pdf").name == "local"
    assert registry.get_text_extractor("text/plain").name == "local"

    # The error must name the setting that fixes it, not just say "unsupported".
    with pytest.raises(ExtractionError, match="TEXTRACT_ENABLED"):
        registry.get_text_extractor("image/png")


def test_a_digital_pdf_prefers_the_local_reader_over_textract(monkeypatch) -> None:
    """Paying Textract to re-read characters already in the file is pure waste."""
    from app.core.config import settings
    from app.services.ai import registry

    monkeypatch.setattr(settings, "TEXTRACT_ENABLED", True)
    monkeypatch.setattr(settings, "OCR_PROVIDER", "auto")
    registry.reset_provider_cache()

    assert registry.get_text_extractor("application/pdf").name == "local"
    # An image has no text layer, so it genuinely needs OCR.
    assert registry.get_text_extractor("image/png").name == "textract"

    registry.reset_provider_cache()


def test_forcing_the_local_provider_rejects_images(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.ai import registry

    monkeypatch.setattr(settings, "OCR_PROVIDER", "local")
    registry.reset_provider_cache()

    with pytest.raises(ExtractionError):
        registry.get_text_extractor("image/tiff")

    registry.reset_provider_cache()


def test_extraction_can_be_disabled_entirely(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.ai import registry

    monkeypatch.setattr(settings, "OCR_PROVIDER", "none")
    registry.reset_provider_cache()

    with pytest.raises(AIProviderUnavailableError):
        registry.get_text_extractor("application/pdf")

    registry.reset_provider_cache()


def test_analyzer_falls_back_to_the_rule_engine_without_a_key() -> None:
    from app.services.ai import registry

    registry.reset_provider_cache()
    assert registry.get_analyzer().name == "heuristic"
    # With no OpenAI configured there is nothing to fall back *from*.
    primary, fallback = registry.get_analyzer_with_fallback()
    assert primary.name == "heuristic"
    assert fallback is None


def test_openai_is_selected_when_a_key_is_present(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.ai import registry

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
    registry.reset_provider_cache()

    primary, fallback = registry.get_analyzer_with_fallback()
    assert primary.name == "openai"
    # The rule engine is offered as a fallback so an outage degrades quality only.
    assert fallback is not None and fallback.name == "heuristic"

    registry.reset_provider_cache()


def test_forcing_openai_without_a_key_fails_loudly(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.ai import registry

    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    registry.reset_provider_cache()

    with pytest.raises(AIProviderUnavailableError, match="OPENAI_API_KEY"):
        registry.get_analyzer()

    registry.reset_provider_cache()


def test_provider_description_explains_reduced_capability() -> None:
    from app.services.ai import registry

    registry.reset_provider_cache()
    status = registry.describe_providers()

    assert status.analysis == "heuristic"
    assert status.openai_available is False
    assert status.textract_available is False
    assert any("Textract" in note for note in status.notes)


def test_provider_description_warns_when_textract_lacks_a_bucket(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.ai import registry

    monkeypatch.setattr(settings, "TEXTRACT_ENABLED", True)
    monkeypatch.setattr(settings, "S3_BUCKET", None)
    registry.reset_provider_cache()

    status = registry.describe_providers()
    assert any("S3 bucket" in note for note in status.notes)

    registry.reset_provider_cache()
