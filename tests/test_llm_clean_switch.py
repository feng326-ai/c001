"""LLM 清洗部署角色开关测试。"""

from wxsearch.ai_filters import llm_client


def test_explicit_env_false_overrides_rule_config_true(monkeypatch):
    monkeypatch.setenv("LLM_CLEAN_ENABLED", "false")
    monkeypatch.setattr(llm_client, "_cfg_llm", lambda: {"clean_enabled": True})

    assert llm_client.get_clean_enabled(default=True) is False


def test_explicit_env_true_overrides_rule_config_false(monkeypatch):
    monkeypatch.setenv("LLM_CLEAN_ENABLED", "true")
    monkeypatch.setattr(llm_client, "_cfg_llm", lambda: {"clean_enabled": False})

    assert llm_client.get_clean_enabled(default=False) is True


def test_rule_config_is_used_when_env_is_absent(monkeypatch):
    monkeypatch.delenv("LLM_CLEAN_ENABLED", raising=False)
    monkeypatch.setattr(llm_client, "_cfg_llm", lambda: {"clean_enabled": True})

    assert llm_client.get_clean_enabled(default=False) is True
