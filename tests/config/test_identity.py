"""Observable startup identity changes with policy/assets without exposing them."""

import json

import pytest

from wobblebot.config.identity import runtime_identity

pytestmark = pytest.mark.unit


def test_identity_is_sanitized_and_tracks_config_and_prompt_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WOBBLEBOT_BUILD_REVISION", "a" * 40)
    monkeypatch.setenv("WOBBLEBOT_IMAGE_DIGEST", "sha256:" + "b" * 64)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("private trading prompt")
    (tmp_path / "requirements.lock").write_text("locked bytes")
    config = {"key": "private-api-secret", "advisor": {"prompt_file": str(prompt)}}
    first = runtime_identity(config, "cpu-only")
    rendered = json.dumps(first)
    assert "private" not in rendered and str(tmp_path) not in rendered
    assert first["revision"] == "a" * 40
    assert first["unavailable_assets"] == 0
    prompt.write_text("revised private prompt")
    second = runtime_identity(config, "cpu-only")
    assert first["assets_sha256"] != second["assets_sha256"]
    assert first["resolved_config_sha256"] == second["resolved_config_sha256"]
    config["key"] = "changed-secret"
    assert (
        runtime_identity(config, "cpu-only")["resolved_config_sha256"]
        != first["resolved_config_sha256"]
    )
    prompt.unlink()
    assert runtime_identity(config, "cpu-only")["unavailable_assets"] == 1


def test_untrusted_environment_and_profile_never_enter_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WOBBLEBOT_BUILD_REVISION", "secret\nforged log")
    monkeypatch.setenv("WOBBLEBOT_IMAGE_DIGEST", "secret")
    identity = runtime_identity({}, "secret\nforged log")
    assert identity["revision"] == identity["declared_image_digest"] == "unknown"
    assert "secret" not in json.dumps(identity)
    assert identity["lock_sha256"] == "unavailable"
