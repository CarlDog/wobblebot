"""Prepare N1 files locally; never start containers or move existing data.

Resolves one existing profile, preserves non-path policy, and writes a new
settings/Compose pair plus an explicit database migration map. The output
must not exist. Operators must take consistent backups and populate the new
layout before any separately authorized deployment. No credentials are read.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

import yaml

from wobblebot.config.loader import WobbleBotConfig
from wobblebot.config.logging import configure_logging
from wobblebot.config.resolver import resolve_config

_LOGGER = logging.getLogger("wobblebot.tools.prepare_isolated_deployment")
ROOT = Path(__file__).resolve().parents[1]
OWNERS = ("live", "observe", "news", "advise", "harvest", "operator", "shadow", "sandbox")
# These are filesystem grants only: operator.db still has multiple table writers.
ACCESS = {
    "delivery": {"operator": "rw"},
    "live": {"live": "rw", "operator": "rw", "observe": "ro"},
    "observe": {"observe": "rw", "operator": "rw"},
    "news": {"news": "rw", "operator": "rw"},
    "advise": {"advise": "rw", "operator": "rw", "observe": "ro", "news": "ro", "live": "ro"},
    "harvest": {"harvest": "rw", "operator": "rw"},
    "operator": {role: "rw" if role == "operator" else "ro" for role in OWNERS[:6]},
    "web": {role: "rw" if role == "operator" else "ro" for role in OWNERS[:6]},
    "maintenance": dict.fromkeys(OWNERS, "rw"),
    "tools": dict.fromkeys(OWNERS, "rw"),
}


def _rewrite(value: Any, paths: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite(item, paths) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, paths) for item in value]
    return paths.get(value, value) if isinstance(value, str) else value


def _referenced_roles(value: Any, paths: dict[str, str]) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_referenced_roles(item, paths) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_referenced_roles(item, paths) for item in value))
    return {paths[value]} if isinstance(value, str) and value in paths else set()


def prepare(
    config_path: Path,
    profile: str | None,
    output: Path,
    host_root: Path | None,
    image_ref: str | None = None,
) -> None:
    """Create reviewable files exclusively in a new output directory."""
    if image_ref is not None and not re.fullmatch(
        r"[A-Za-z0-9._/:-]+@sha256:[0-9a-f]{64}", image_ref
    ):
        raise ValueError("Deployment image must use a sha256 digest, not a mutable tag")
    raw = resolve_config(yaml.safe_load(config_path.read_text(encoding="utf-8")), profile)
    WobbleBotConfig.model_validate(raw)
    paths: dict[str, str] = {}
    roles: dict[str, str] = {}
    migration = []
    for role in OWNERS:
        section = raw.get(role)
        if section is None:
            continue
        key = "operator_db" if role == "operator" else "db"
        original = section.get(key)
        if not original or original == ":memory:":
            raise ValueError(f"{role}.{key} must name a persistent database")
        if original in paths:
            raise ValueError("Database owner paths must be distinct before isolation")
        target = f"data/{role}/{Path(original).name}"
        paths[original] = target
        roles[original] = role
        migration.append(
            {
                "role": role,
                "source": original,
                "container_destination": target,
                "host_destination": f"state/{role}/{Path(original).name}",
            }
        )
    for role in ACCESS:
        if role not in {"tools", "delivery"} and raw.get(role) is None:
            raise ValueError(f"The full deployment requires the {role} section")

    # Reject unclassified database targets; guessing ownership would widen authority.
    def check_db_paths(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for name, item in value.items():
                check_db_paths(item, name)
        elif isinstance(value, list):
            for item in value:
                check_db_paths(item, key)
        elif value is not None and (key == "db" or key.endswith("_db") or key == "target_dbs"):
            if value not in roles:
                raise ValueError(f"Unclassified database path in {key}; assign an owner first")

    check_db_paths(raw)

    def check_asset_paths(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for name, item in value.items():
                check_asset_paths(item, name)
        elif isinstance(value, list):
            for item in value:
                check_asset_paths(item, key)
        elif value is not None and key in {"prompt_file", "heuristic_file"}:
            asset = Path(value)
            if (
                asset.is_absolute()
                or ".." in asset.parts
                or len(asset.parts) < 3
                or asset.parts[:2] not in {("config", "prompts"), ("config", "heuristic")}
            ):
                raise ValueError(f"{key} must be inside config/prompts or config/heuristic")
            source = config_path.parent.joinpath(*asset.parts[1:])
            if not source.is_file():
                raise ValueError(f"Required {key} file is missing")

    check_asset_paths(raw)
    settings = _rewrite(raw, paths)
    for service in ACCESS:
        if service in settings and settings[service].get("log_file_path") is not None:
            settings[service]["log_file_path"] = f"data/logs/{service}/{service}.log"
    settings["maintenance"]["archive_dir"] = "data/archive"
    settings["maintenance"]["backup_dir"] = "data/backups"
    WobbleBotConfig.model_validate(settings)
    template = yaml.safe_load((ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8"))
    compose = {"services": copy.deepcopy(template["services"])}
    if raw.get("delivery") is None:
        compose["services"].pop("delivery", None)
    host = (host_root or output).absolute()
    for name, service in compose["services"].items():
        service["environment"]["WOBBLEBOT_BOOTSTRAP_CONFIG"] = "0"
        if image_ref is not None:
            service["image"] = image_ref
            service["environment"]["WOBBLEBOT_IMAGE_DIGEST"] = image_ref.split("@", 1)[1]
        required = set(roles.values()) if name == "tools" else _referenced_roles(raw[name], roles)
        # Advisor's cost ledger is configured in operator, not advise.
        if name == "advise" and raw.get("llm") is not None:
            required.add("operator")
        permitted = ACCESS[name]
        if required - permitted.keys():
            raise ValueError(f"{name} has a database consumer outside the capability contract")
        mounts = [
            {
                "type": "bind",
                "source": str(host / "state" / role),
                "target": f"/app/data/{role}",
                "read_only": permitted[role] == "ro",
            }
            for role in sorted(required)
        ]
        mounts += [
            {
                "type": "bind",
                "source": str(host / "config"),
                "target": "/app/config",
                "read_only": name != "tools",
            },
            {
                "type": "bind",
                "source": str(host / "logs" / name),
                "target": f"/app/data/logs/{name}",
                "read_only": False,
            },
        ]
        if name in {"maintenance", "tools"}:
            mounts += [
                {
                    "type": "bind",
                    "source": str(host / area),
                    "target": f"/app/data/{area}",
                    "read_only": False,
                }
                for area in ("archive", "backups")
            ]
        service["volumes"] = mounts
        if name != "tools":
            command = shlex.split(service["command"])
            index = command.index("--profile")
            command[index : index + 2] = ["--config", "/app/config/settings.yml"]
            service["command"] = command
        if "healthcheck" in service and "--profile" in service["healthcheck"]["test"]:
            command = service["healthcheck"]["test"]
            index = command.index("--profile")
            command[index : index + 2] = ["--config", "/app/config/settings.yml"]
    # Validate everything before creating output; never overwrite an existing plan.
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    (output / "config").mkdir()
    for area in ("prompts", "heuristic"):
        source = config_path.parent / area
        if source.is_dir():
            shutil.copytree(source, output / "config" / area)
    for role in roles.values():
        (output / "state" / role).mkdir(parents=True)
    for name in ACCESS:
        (output / "logs" / name).mkdir(parents=True)
    for area in ("archive", "backups"):
        (output / area).mkdir()
    (output / "config/settings.yml").write_text(
        yaml.safe_dump(settings, sort_keys=False), encoding="utf-8"
    )
    (output / "compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    (output / "migration-map.json").write_text(
        json.dumps(migration, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    """Generate only; deployment and copying live data require separate actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-root", type=Path, help="Final host path; no writes are made there")
    parser.add_argument("--image", help="Verified image reference pinned by @sha256 digest")
    args = parser.parse_args()
    configure_logging()
    try:
        prepare(args.config, args.profile, args.output, args.host_root, args.image)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        _LOGGER.error("Cannot prepare isolated deployment: %s", exc)
        return 2
    _LOGGER.info(
        "Isolated deployment prepared at %s; databases not copied, no services started", args.output
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
