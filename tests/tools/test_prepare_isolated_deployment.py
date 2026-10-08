"""N1 generator preserves policy, refuses unsafe guesses and narrows grants."""

import os
from pathlib import Path

import pytest
import yaml
from tools.prepare_isolated_deployment import prepare

from wobblebot.config.runtime import load_resolved_config

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config/settings.example.yml"


def test_generated_contract_preserves_policy_and_denies_extra_grants(tmp_path):
    output = tmp_path / "isolated"
    before = EXAMPLE.read_bytes()
    prepare(EXAMPLE, "cpu-only", output, None)
    original = load_resolved_config(EXAMPLE, "cpu-only")
    generated = load_resolved_config(output / "config/settings.yml")
    assert generated.grid == original.grid
    assert generated.safety == original.safety
    assert generated.advisor == original.advisor
    assert EXAMPLE.read_bytes() == before
    assert not list((output / "state").rglob("*.db"))
    compose = yaml.safe_load((output / "compose.yml").read_text())
    services = compose["services"]
    assert not any(
        key in services["web"]["environment"]
        for key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "KRAKEN_TRADER_API_KEY",
            "KRAKEN_HARVESTER_API_KEY",
        )
    )
    for name, service in services.items():
        assert all(mount["target"] != "/app/data" for mount in service["volumes"])
        (config,) = [mount for mount in service["volumes"] if mount["target"] == "/app/config"]
        assert config["read_only"] == (name != "tools")
    (web_live,) = [
        mount for mount in services["web"]["volumes"] if mount["target"] == "/app/data/live"
    ]
    assert web_live["read_only"]
    assert not any(mount["target"] == "/app/data/harvest" for mount in services["live"]["volumes"])
    assert services["live"]["restart"] == "no"
    assert services["harvest"]["restart"] == "no"
    assert services["tools"]["healthcheck"] == {"disable": True}
    assert services["delivery"]["healthcheck"]["test"][3:] == [
        "--daemon",
        "cli/delivery",
        "--config",
        "/app/config/settings.yml",
    ]
    with pytest.raises(FileExistsError):
        prepare(EXAMPLE, "cpu-only", output, None)


def test_unknown_or_shared_database_owner_is_rejected_before_output(tmp_path):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["web"]["live_db"] = "data/unclassified.db"
    config = tmp_path / "settings.yml"
    config.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="Unclassified"):
        prepare(config, None, tmp_path / "out", None)
    assert not (tmp_path / "out").exists()
    raw["web"]["live_db"] = raw["live"]["db"]
    raw["harvest"]["db"] = raw["live"]["db"]
    config.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="distinct"):
        prepare(config, None, tmp_path / "out", None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX container entrypoint; exercised on Linux")
def test_reader_bootstrap_does_not_write_staged_config(tmp_path):
    import subprocess

    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "missing.yml").write_text("private: unchanged\n")
    target = tmp_path / "target"
    target.mkdir()
    script = (ROOT / "docker/entrypoint.sh").read_text()
    script = script.replace("DEFAULTS=/opt/wobblebot/defaults/config", f"DEFAULTS={defaults}")
    script = script.replace("TARGET=/app/config", f"TARGET={target}")
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text(script)
    subprocess.run(
        ["sh", str(entrypoint), "true"],
        env={**os.environ, "WOBBLEBOT_BOOTSTRAP_CONFIG": "0"},
        check=True,
        timeout=10,
    )
    assert list(target.iterdir()) == []


def test_deployment_image_must_be_digest_pinned(tmp_path):
    with pytest.raises(ValueError, match="digest"):
        prepare(EXAMPLE, "cpu-only", tmp_path / "bad", None, "example.invalid/app:latest")
    assert not (tmp_path / "bad").exists()
    image = "example.invalid/app@sha256:" + "a" * 64
    prepare(EXAMPLE, "cpu-only", tmp_path / "good", None, image)
    services = yaml.safe_load((tmp_path / "good/compose.yml").read_text())["services"]
    assert all(service["image"] == image for service in services.values())
    assert all(
        service["environment"]["WOBBLEBOT_IMAGE_DIGEST"] == "sha256:" + "a" * 64
        for service in services.values()
    )


@pytest.mark.parametrize(
    "image",
    [
        "-" * 100000,
        "registry/app@sha256:" + "a" * 63,
        "registry/app@sha256:" + "a" * 65,
        "registry/app@sha256:" + "A" * 64,
        "registry/app@sha256:" + "g" * 64,
        "registry/app@sha256:" + "a" * 64 + "\n",
        "registry/app@sha256:@sha256:" + "a" * 64,
        "registry/app?tag@sha256:" + "a" * 64,
        "@sha256:" + "a" * 64,
    ],
    ids=[
        "long-missing-digest",
        "short-digest",
        "long-digest",
        "uppercase-digest",
        "nonhex-digest",
        "trailing-newline",
        "duplicate-delimiter",
        "invalid-repository-character",
        "empty-repository",
    ],
)
def test_invalid_image_reference_does_not_read_config_or_create_output(tmp_path, image):
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="digest"):
        prepare(tmp_path / "absent.yml", None, output, None, image)
    assert not output.exists()


def test_digest_reference_preserves_registry_port_tag_and_punctuation(tmp_path):
    image = "Registry.example:5000/team/my_app-v2.1:alpha@sha256:" + "0123456789abcdef" * 4
    output = tmp_path / "output"
    prepare(EXAMPLE, "cpu-only", output, None, image)
    services = yaml.safe_load((output / "compose.yml").read_text())["services"]
    assert all(service["image"] == image for service in services.values())
