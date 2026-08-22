import json
import os
import stat
from pathlib import Path

import pytest

from wxsearch.config import load_config, save_config


ROOT = Path(__file__).resolve().parents[1]


def test_missing_runtime_config_fails_closed(tmp_path: Path):
    missing = tmp_path / "config.json"

    with pytest.raises(FileNotFoundError, match="config.example.json"):
        load_config(str(missing))


def test_tracked_example_is_safe_and_disabled():
    example = ROOT / "config.example.json"
    raw = json.loads(example.read_text(encoding="utf-8"))
    config = load_config(str(example))

    assert raw["keywords"] == []
    assert config.keywords == []
    assert config.distributed.enabled is False
    assert config.unattended.enabled is False
    assert config.unattended.vm_instance_id == "replace-with-unique-vm-id"
    assert "replace-with-runtime-password" in config.distributed.broker_url


def test_existing_partial_config_still_uses_field_defaults(tmp_path: Path):
    runtime = tmp_path / "config.json"
    runtime.write_text(
        json.dumps({"keywords": ["评选征集"]}, ensure_ascii=False),
        encoding="utf-8",
    )

    config = load_config(str(runtime))

    assert config.keywords == ["评选征集"]
    assert config.collect.max_scrolls == 30
    assert config.selectors.filter_type == "文章"


def _enabled_runtime_config() -> dict:
    return {
        "keywords": [],
        "distributed": {
            "enabled": True,
            "broker_url": "redis://collector.internal:6379/0",
            "result_backend": "redis://collector.internal:6379/1",
        },
        "unattended": {
            "enabled": True,
            "vm_instance_id": "wxg-test-01",
        },
    }


@pytest.mark.parametrize(
    "vm_instance_id",
    [
        None,
        "vm-01",
        "VM-01",
        "Vm-01",
        "replace-with-unique-vm-id",
        "your_vm_id",
        "your-vm-id",
        "change-me",
        "changeme",
        "placeholder",
    ],
)
def test_unattended_requires_explicit_non_placeholder_identity(
    tmp_path: Path, vm_instance_id: str | None
):
    runtime = tmp_path / "config.json"
    raw = _enabled_runtime_config()
    if vm_instance_id is None:
        del raw["unattended"]["vm_instance_id"]
    else:
        raw["unattended"]["vm_instance_id"] = vm_instance_id
    runtime.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="vm_instance_id"):
        load_config(str(runtime))


def test_enabled_distributed_mode_requires_explicit_runtime_urls(tmp_path: Path):
    runtime = tmp_path / "config.json"
    raw = _enabled_runtime_config()
    del raw["distributed"]["result_backend"]
    runtime.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="result_backend"):
        load_config(str(runtime))


@pytest.mark.parametrize(
    "placeholder_url",
    [
        "redis://:changeme@collector.internal:6379/1",
        "redis://:change%2Dme@collector.internal:6379/1",
        "redis://:placeholder@collector.internal:6379/1",
        "redis://redis.example.com:6379/1",
    ],
)
def test_enabled_distributed_mode_rejects_placeholder_urls(
    tmp_path: Path, placeholder_url: str
):
    runtime = tmp_path / "config.json"
    raw = _enabled_runtime_config()
    raw["distributed"]["result_backend"] = placeholder_url
    runtime.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="result_backend"):
        load_config(str(runtime))


def test_valid_enabled_runtime_contract_loads(tmp_path: Path):
    runtime = tmp_path / "config.json"
    runtime.write_text(json.dumps(_enabled_runtime_config()), encoding="utf-8")

    config = load_config(str(runtime))

    assert config.distributed.enabled is True
    assert config.unattended.vm_instance_id == "wxg-test-01"


def test_save_config_is_atomic_and_private(tmp_path: Path):
    runtime = tmp_path / "config.json"

    save_config({"keywords": ["评选征集"]}, str(runtime))

    assert json.loads(runtime.read_text(encoding="utf-8"))["keywords"] == ["评选征集"]
    assert not list(tmp_path.glob(".config-*.json.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o600
