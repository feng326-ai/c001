import asyncio
import importlib
import json
import os
import stat
import sys

import pytest
from fastapi import HTTPException

from wxsearch.ai_filters.llm_client import save_secret_api_key
from wxsearch.api import main as api_main
from wxsearch.api.auth import require_super


def test_model_probe_route_is_post_and_requires_super():
    route = next(
        route
        for route in api_main.app.routes
        if getattr(route, "path", None) == "/api/v1/settings/models"
    )

    assert route.methods == {"POST"}
    assert any(
        dependency.call is require_super
        for dependency in route.dependant.dependencies
    )


def test_custom_model_probe_host_is_allowlisted_https(monkeypatch):
    monkeypatch.setenv("LLM_PROBE_ALLOWED_HOSTS", "models.example.com")

    assert api_main._validate_model_probe_base_url(
        "https://models.example.com/v1", "http://configured.internal/v1"
    ) == "https://models.example.com/v1"
    with pytest.raises(HTTPException):
        api_main._validate_model_probe_base_url(
            "https://attacker.example/v1", "http://configured.internal/v1"
        )
    with pytest.raises(HTTPException):
        api_main._validate_model_probe_base_url(
            "http://models.example.com/v1", "http://configured.internal/v1"
        )


def test_custom_model_probe_never_inherits_server_key(monkeypatch):
    class ConfiguredClient:
        base_url = "http://configured.internal/v1"

        def list_models(self):
            return ["configured"]

    monkeypatch.setenv("LLM_PROBE_ALLOWED_HOSTS", "models.example.com")
    monkeypatch.setattr(
        "wxsearch.ai_filters.llm_client.get_client",
        lambda: ConfiguredClient(),
    )

    with pytest.raises(HTTPException, match="临时密钥"):
        asyncio.run(
            api_main.list_available_models(
                api_main.ModelProbeRequest(
                    base_url="https://models.example.com/v1"
                ),
                current_user={"role": "super"},
            )
        )


def test_secret_file_write_is_atomic_and_private(tmp_path, monkeypatch):
    secret_path = tmp_path / "runtime" / "secrets.json"
    monkeypatch.setenv("WXSEARCH_SECRETS_PATH", str(secret_path))

    save_secret_api_key("temporary-test-value")

    assert json.loads(secret_path.read_text(encoding="utf-8"))["api_key"] == "temporary-test-value"
    assert not list(secret_path.parent.glob(".secrets-*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600


def test_sogou_log_token_uses_header_and_database_url_is_not_required(
    monkeypatch,
):
    monkeypatch.setenv("REDIS_URL", "redis://placeholder/0")
    monkeypatch.setenv("API_BASE", "https://collector.example")
    monkeypatch.setenv("SOGOU_API_TOKEN", "temporary-test-token")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sys.modules.pop("wxsearch.sogou_loop", None)
    module = importlib.import_module("wxsearch.sogou_loop")

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)

    monkeypatch.setattr("requests.post", fake_post)
    handler = module.RemoteLogHandler(
        "sogou-test", "https://collector.example", "temporary-test-token"
    )
    handler._buf = [
        {"device_id": "sogou-test", "level": "INFO", "message": "ok"}
    ]
    handler.flush()

    assert "?" not in captured["url"]
    assert captured["headers"] == {
        "X-Collect-Token": "temporary-test-token"
    }


def test_collect_log_query_token_is_disabled_by_default(monkeypatch):
    test_value = "-".join(("temporary", "test", "value"))
    monkeypatch.delenv("ALLOW_LEGACY_COLLECT_LOG_QUERY", raising=False)
    monkeypatch.setattr(api_main, "_COLLECT_LOG_TOKEN", test_value)

    with pytest.raises(HTTPException, match="令牌无效"):
        asyncio.run(
            api_main.report_collect_logs(
                api_main.CollectLogBatch(logs=[]),
                x_collect_token=None,
                x_token=test_value,
            )
        )


def test_collect_log_query_token_requires_explicit_compatibility_window(
    monkeypatch, caplog
):
    test_value = "-".join(("temporary", "test", "value"))
    monkeypatch.setenv("ALLOW_LEGACY_COLLECT_LOG_QUERY", "1")
    monkeypatch.setattr(api_main, "_COLLECT_LOG_TOKEN", test_value)

    result = asyncio.run(
        api_main.report_collect_logs(
            api_main.CollectLogBatch(logs=[]),
            x_collect_token=None,
            x_token=test_value,
        )
    )

    assert result == {"inserted": 0}
    assert "旧版 query" in caplog.text
