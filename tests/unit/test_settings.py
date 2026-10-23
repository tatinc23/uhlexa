import pytest

from app.settings import Settings, get_settings

ENV_NAMES = [name.upper() for name in Settings.model_fields]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_provider_flags_default_off(clean_env: None) -> None:
    s = Settings(_env_file=None)
    assert not any(
        (s.enable_primitive, s.enable_subq_snap, s.enable_maritime, s.enable_keiki, s.enable_inkbox, s.enable_ring)
    )
    assert s.enable_firetv is False
    assert s.subq_snap_model == "subq-snap-preview"
    assert s.maritime_mcp_url == "https://mcp.maritime.sh/mcp"
    assert s.subq_decisions_base_url == "https://api.subq.ai/v1"


def test_defaults_match_spec(clean_env: None) -> None:
    s = Settings(_env_file=None)
    assert s.is_development and s.app_env == "development"
    assert (s.app_host, s.app_port) == ("0.0.0.0", 8000)
    assert s.enable_demo_auth and s.enable_approvals
    assert s.demo_default_user == "alex"
    assert s.job_lease_seconds == 120
    assert s.embedded_worker is False
    assert s.primitive_api_key == "" and s.subq_api_key == "" and s.maritime_computers_key == ""


def test_keiki_inkbox_defaults_match_the_addendum(clean_env: None) -> None:
    s = Settings(_env_file=None)
    assert s.keiki_api_base_url == "https://onkeiki.com"
    assert s.keiki_gateway_key == "" and s.keiki_api_key == "" and s.keiki_agent_ids == {}
    assert (s.keiki_gateway_timeout_s, s.keiki_call_reasoning, s.keiki_daily_turn_cap) == (120, "none", 200)
    assert s.inkbox_base_url == "https://inkbox.ai"
    assert s.inkbox_api_key == "" and s.inkbox_signing_keys == {} and s.inkbox_webhook_auth_token == ""
    assert not any((s.enable_inkbox_imessage, s.enable_inkbox_sms, s.enable_inkbox_email, s.enable_inkbox_calls))
    assert not any((s.enable_outbound_contact, s.enable_outbound_calls, s.enable_channel_approvals))
    assert (s.outbound_texts_per_hour, s.outbound_calls_per_hour, s.outbound_per_day) == (6, 2, 20)


def test_json_maps_parse_from_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEIKI_AGENT_IDS", '{"Alex": "agent-a", "katie": "agent-j"}')
    monkeypatch.setenv("INKBOX_SIGNING_KEYS", '{"parker-alex": "whsec_x"}')
    s = Settings(_env_file=None)
    assert s.keiki_agent_ids == {"alex": "agent-a", "katie": "agent-j"}  # handles are lower-cased
    assert s.inkbox_signing_keys == {"parker-alex": "whsec_x"}


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_json_map_is_empty(clean_env: None, monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("KEIKI_AGENT_IDS", blank)
    assert Settings(_env_file=None).keiki_agent_ids == {}


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("KEIKI_AGENT_IDS", "{not json SENTINEL-agent-secret"),
        ("KEIKI_AGENT_IDS", '["SENTINEL-agent-secret"]'),
        ("KEIKI_AGENT_IDS", '{"alex": 7, "x": "SENTINEL-agent-secret"}'),
        ("INKBOX_SIGNING_KEYS", "whsec_SENTINEL-agent-secret"),
        ("INKBOX_SIGNING_KEYS", '{"h": ["SENTINEL-agent-secret"]}'),
    ],
)
def test_bad_json_map_error_names_variable_not_value(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, name: str, bad: str
) -> None:
    monkeypatch.setenv(name, bad)
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None)
    text = str(caught.value) + repr(caught.value.args)
    assert name in text
    assert "SENTINEL-agent-secret" not in text


@pytest.mark.parametrize("name", ["KEIKI_API_BASE_URL", "INKBOX_BASE_URL"])
def test_base_urls_must_be_https_except_localhost(clean_env: None, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "http://example.com")
    with pytest.raises(ValueError, match=name):
        Settings(_env_file=None)
    monkeypatch.setenv(name, "http://localhost:9999")
    assert Settings(_env_file=None)
    monkeypatch.setenv(name, "https://example.com")
    assert Settings(_env_file=None)


def test_keiki_call_reasoning_is_restricted(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEIKI_CALL_REASONING", "extreme")
    with pytest.raises(ValueError, match="KEIKI_CALL_REASONING"):
        Settings(_env_file=None)
    monkeypatch.setenv("KEIKI_CALL_REASONING", "low")
    assert Settings(_env_file=None).keiki_call_reasoning == "low"


def test_env_overrides_and_case_insensitive(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_SUBQ_SNAP", "true")
    monkeypatch.setenv("APP_PORT", "9001")
    s = Settings(_env_file=None)
    assert s.enable_subq_snap is True and s.app_port == 9001


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_database_url_is_normalised_for_psycopg3(clean_env: None, monkeypatch: pytest.MonkeyPatch, scheme: str) -> None:
    monkeypatch.setenv("DATABASE_URL", f"{scheme}://u:p@host/db?sslmode=require")
    assert Settings(_env_file=None).database_url == "postgresql+psycopg://u:p@host/db?sslmode=require"


def test_explicit_driver_url_is_untouched(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    assert Settings(_env_file=None).database_url == "sqlite:///x.db"


def test_production_refuses_the_default_session_secret(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="APP_SESSION_SECRET"):
        Settings(_env_file=None)
    monkeypatch.setenv("APP_SESSION_SECRET", "a-real-secret")
    assert Settings(_env_file=None).app_session_secret == "a-real-secret"


def test_blank_session_secret_falls_back_outside_production(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SESSION_SECRET", "")
    assert Settings(_env_file=None).app_session_secret


def test_secrets_are_not_in_repr(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBQ_API_KEY", "sentinel-subq")
    monkeypatch.setenv("MARITIME_COMPUTERS_KEY", "sentinel-maritime")
    monkeypatch.setenv("PRIMITIVE_API_KEY", "sentinel-primitive")
    monkeypatch.setenv("APP_SESSION_SECRET", "sentinel-session")
    monkeypatch.setenv("KEIKI_GATEWAY_KEY", "sentinel-keiki-gw")
    monkeypatch.setenv("KEIKI_API_KEY", "sentinel-keiki-admin")
    monkeypatch.setenv("INKBOX_API_KEY", "sentinel-inkbox")
    monkeypatch.setenv("INKBOX_WEBHOOK_AUTH_TOKEN", "sentinel-inkbox-hook")
    monkeypatch.setenv("INKBOX_SIGNING_KEYS", '{"h": "sentinel-signing"}')
    text = repr(Settings(_env_file=None))
    assert "sentinel" not in text


def test_get_settings_is_cached_until_cleared(settings_env: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_settings() is get_settings()
    monkeypatch.setenv("ENABLE_APPROVALS", "false")
    assert get_settings().enable_approvals is True
    get_settings.cache_clear()
    assert get_settings().enable_approvals is False


@pytest.mark.parametrize(
    ("name", "default"), [("KEIKI_API_BASE_URL", "https://onkeiki.com"), ("INKBOX_BASE_URL", "https://inkbox.ai")]
)
def test_blank_base_url_means_the_default(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, name: str, default: str
) -> None:
    monkeypatch.setenv(name, "")
    s = Settings(_env_file=None)
    assert default in (s.keiki_api_base_url, s.inkbox_base_url)
