"""ADR-041 — the deployment capability matrix, asserted against the compose file.

WHY THIS TEST EXISTS. ADR-003 says the withdrawal-capable Harvester key
lives only with the Harvester. Until ADR-041 that was true of the *Python*
and false of the *deployment*: one ``x-wobblebot-defaults`` YAML anchor
injected every Kraken key — reader, trader, AND the withdrawal-enabled
Harvester key — plus every cloud-LLM key, the Discord token, and the web
session secret into all nine services. The invariant the architecture is
built around stopped at the language boundary.

WHAT IT CAN AND CANNOT PROVE. This asserts *credential presence* and
*mount mode* — what a container is handed. It cannot prove *semantic*
authority: placing an order, approving a command, rewriting settings, or
initiating a transfer are enforced by the application and by the storage
contract, and their tests live elsewhere. Read this as one layer, not the
whole guarantee.

THE MATRIX IS AN ALLOWLIST, DELIBERATELY. Each service asserts the exact
set it holds, not a subset — so re-adding a credential to a shared anchor
fails here rather than passing quietly. ``test_no_secret_lives_in_a_shared_anchor``
is the structural backstop for the same mistake in its original form.

MAPPING PROVENANCE. Every row was verified against the source, NOT against
the compose file's own comments — which had drifted in three places by
2026-08-28 (they claimed ``cli/operator`` needed the reader key for a
BalanceEx path it does not have, and ``cli/harvest``'s module docstring
still described the Stage-4.2 reader-key posture that Stage 4.4 replaced).
Citations are on each row below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"

# Every env var in the compose file that carries a secret. A name that is
# not in this set is treated as non-secret runtime wiring (OLLAMA_BASE_URL,
# IMAGE_TAG, HOST_*_DIR) and is exempt from the matrix.
CREDENTIAL_ENV_KEYS = frozenset(
    {
        "KRAKEN_READER_API_KEY",
        "KRAKEN_READER_API_SECRET",
        "KRAKEN_TRADER_API_KEY",
        "KRAKEN_TRADER_API_SECRET",
        "KRAKEN_HARVESTER_API_KEY",
        "KRAKEN_HARVESTER_API_SECRET",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "ATLASCLOUD_API_KEY",
        "CRYPTOCOMPARE_API_KEY",
        "DISCORD_BOT_TOKEN",
        "WOBBLEBOT_WEB_SESSION_SECRET",
    }
)

_READER = frozenset({"KRAKEN_READER_API_KEY", "KRAKEN_READER_API_SECRET"})
_TRADER = frozenset({"KRAKEN_TRADER_API_KEY", "KRAKEN_TRADER_API_SECRET"})
_HARVESTER = frozenset({"KRAKEN_HARVESTER_API_KEY", "KRAKEN_HARVESTER_API_SECRET"})
_CLOUD_LLM = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"})

# service -> the EXACT credential set it may hold, with the source that
# establishes the need. Anything absent from a row is a credential that
# service must not receive.
EXPECTED_CREDENTIALS: dict[str, frozenset[str]] = {
    # cli/live.py:1954 — KrakenConfig.from_env(key_var="KRAKEN_TRADER_API_KEY", …).
    # Notifications go to the DB for cli/operator to forward, so no Discord token.
    "live": _TRADER,
    # cli/observe.py:460 — KrakenConfig.from_env() with the reader defaults.
    "observe": _READER,
    # cli/news.py:78 — the only credential it reads. No Kraken adapter at all
    # (the Kraken status feed is public). CryptoCompare is retired upstream and
    # off by default; the var stays wired for an operator with a paid plan.
    "news": frozenset({"CRYPTOCOMPARE_API_KEY"}),
    # cli/advise.py:128-131 — _CLOUD_KEY_ENV. No Kraken adapter; Ollama needs
    # no credential.
    "advise": _CLOUD_LLM | {"ATLASCLOUD_API_KEY"},
    # cli/harvest.py:432 — the ONLY adapter it builds reads
    # config.harvester.api_key_env_var (default KRAKEN_HARVESTER_API_KEY),
    # daemon mode included. The module docstring's "uses the read-only
    # KRAKEN_READER_API_KEY" is frozen at Stage 4.2 and no longer true.
    #
    # The trader key is deliberately ABSENT even though harvest.py:323
    # (_TRADE_KEY_ENV_VAR) byte-compares against it to prove the two keys
    # differ. That check degrades by design when the trade key is not in the
    # environment — and its absence IS the separation the check approximates,
    # so removing it strengthens the invariant rather than weakening it.
    "harvest": _HARVESTER,
    # cli/operator.py:1127/1154/1183 — the assistant's cloud keys — plus the
    # Discord token. NO Kraken credential: the USD balance comes from
    # observe.db's balance_snapshots (operator.py:530-535) and a
    # MockExchangeAdapter is injected at operator.py:1399.
    "operator": _CLOUD_LLM | {"DISCORD_BOT_TOKEN"},
    # cli/web.py:245-247 — the cloud keys drive the /health LLM card's
    # ok/unauthorized/not-configured badge (non-billable GET /v1/models
    # probes). NO Kraken credential: web.py:229 builds a bare httpx client for
    # public probes only, which is ADR-016/017's credential-free web tier.
    #
    # ACCEPTED RESIDUAL (ADR-041): web is the reverse-proxied service and it
    # holds three billable keys to render a badge. Dropping them would make
    # the card report "not configured" for providers that ARE configured,
    # which is worse than no card. Sourcing that status from a daemon that
    # already holds the keys is a named 2.1 follow-up, not a silent change here.
    "web": _CLOUD_LLM | {"WOBBLEBOT_WEB_SESSION_SECRET"},
    # cli/maintenance.py:116-118 imports the capital / ledger / reconcile
    # cycles, each of which calls KrakenConfig.from_env(key_var=
    # "KRAKEN_READER_API_KEY", …).
    "maintenance": _READER,
    # The on-demand one-shot dispatcher: cli/preflight needs the trader key,
    # cli/status + cli/recalibrate the reader key, tools/run_cloud_check.py the
    # cloud keys. NOT the Harvester key — `cli/harvest --execute` runs against
    # the `harvest` service (`docker compose run --rm harvest …`), which keeps
    # withdrawal authority in exactly one service definition.
    "tools": _READER | _TRADER | _CLOUD_LLM | {"ATLASCLOUD_API_KEY"},
}

# Non-secret wiring every service must still receive after the split. Present
# to catch the specific way this refactor fails: a per-service `environment:`
# block replaces the anchor's wholesale (YAML merge keys do not deep-merge),
# so an omitted `<<: *common-env` silently drops these.
REQUIRED_COMMON_ENV = frozenset({"OLLAMA_BASE_URL"})

# Only the authorized settings writer gets a writable config mount.
# services/settings_rewriter.py is reached from exactly two callers —
# cli/apply.py:54 and cli/recalibrate.py:60 — and both are one-shot CLIs run
# through the `tools` service. Every daemon reads config and never writes it.
CONFIG_WRITERS = frozenset({"tools"})


def _load_compose() -> dict[str, Any]:
    """Parse the compose file with YAML merge keys resolved."""
    with _COMPOSE_PATH.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict), "compose file did not parse as a mapping"
    return loaded


@pytest.fixture(name="compose", scope="module")
def compose_fixture() -> dict[str, Any]:
    return _load_compose()


@pytest.fixture(name="services", scope="module")
def services_fixture(compose: dict[str, Any]) -> dict[str, Any]:
    services = compose.get("services")
    assert isinstance(services, dict) and services, "compose file declares no services"
    return services


def _credentials_of(service: dict[str, Any]) -> frozenset[str]:
    environment = service.get("environment") or {}
    assert isinstance(environment, dict), (
        "`environment:` must be a mapping, not a list — the matrix and the "
        "`<<: *common-env` merge both depend on the mapping form"
    )
    return frozenset(environment) & CREDENTIAL_ENV_KEYS


def test_every_service_is_covered_by_the_matrix(services: dict[str, Any]) -> None:
    """A new service must be given an explicit row, not inherit one silently."""
    assert set(services) == set(EXPECTED_CREDENTIALS), (
        "compose services and the ADR-041 matrix disagree; add the new service "
        "to EXPECTED_CREDENTIALS with the source that establishes what it needs"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_CREDENTIALS))
def test_service_holds_exactly_its_declared_credentials(
    services: dict[str, Any], name: str
) -> None:
    """Allowlist, not subset — an extra credential fails just as loudly as a missing one."""
    actual = _credentials_of(services[name])
    expected = EXPECTED_CREDENTIALS[name]
    assert actual == expected, (
        f"service {name!r} credential grant drifted from ADR-041.\n"
        f"  unexpected: {sorted(actual - expected)}\n"
        f"  missing:    {sorted(expected - actual)}"
    )


def test_withdrawal_credential_reaches_exactly_one_service(services: dict[str, Any]) -> None:
    """ADR-003's load-bearing invariant, asserted at the container boundary."""
    holders = sorted(name for name, svc in services.items() if _credentials_of(svc) & _HARVESTER)
    assert holders == ["harvest"], (
        "the withdrawal-enabled Harvester key must reach the harvest service and "
        f"nothing else (ADR-003); found: {holders}"
    )


def test_no_service_holds_both_trading_and_withdrawal_authority(
    services: dict[str, Any],
) -> None:
    """The separation is the point: one compromise must not yield both powers."""
    for name, svc in services.items():
        creds = _credentials_of(svc)
        assert not (creds & _TRADER and creds & _HARVESTER), (
            f"service {name!r} holds both the trade key and the withdrawal key — "
            "ADR-003 forbids co-locating them"
        )


def test_no_secret_lives_in_a_shared_anchor(compose: dict[str, Any]) -> None:
    """The structural backstop.

    The original defect was not a wrong matrix row — it was a *shared* block
    holding secrets, so every consumer inherited them. Guard the shape, not
    just the current values: a future ``x-`` anchor must not carry a
    credential even if today's matrix happens to stay correct.
    """
    for key, block in compose.items():
        if not key.startswith("x-") or not isinstance(block, dict):
            continue
        environment = block.get("environment")
        candidates = set(block) | (set(environment) if isinstance(environment, dict) else set())
        leaked = sorted(candidates & CREDENTIAL_ENV_KEYS)
        assert not leaked, (
            f"shared anchor {key!r} carries credentials {leaked} — every service "
            "merging it inherits them. Move secrets to the per-service "
            "`environment:` blocks the ADR-041 matrix describes."
        )


@pytest.mark.parametrize("name", sorted(EXPECTED_CREDENTIALS))
def test_service_keeps_the_common_non_secret_env(services: dict[str, Any], name: str) -> None:
    """YAML merge keys replace, they don't deep-merge — catch a dropped `<<: *common-env`."""
    environment = services[name].get("environment") or {}
    missing = sorted(REQUIRED_COMMON_ENV - set(environment))
    assert not missing, (
        f"service {name!r} lost common runtime env {missing} — a per-service "
        "`environment:` block REPLACES the anchor's, so it needs `<<: *common-env`"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_CREDENTIALS))
def test_config_is_read_only_except_for_the_authorized_writer(
    services: dict[str, Any], name: str
) -> None:
    """Steady-state config is read-only; only the settings-rewriting CLI may write it."""
    mounts = services[name].get("volumes") or []
    # Match on the suffix, not a `:`-split: the host side is
    # `${HOST_CONFIG_DIR:-/volume1/…}`, whose own `:-` default syntax
    # contains a colon and shifts every positional index.
    config_mounts = [
        m
        for m in mounts
        if isinstance(m, str) and (m.endswith(":/app/config") or m.endswith(":/app/config:ro"))
    ]
    assert (
        len(config_mounts) == 1
    ), f"service {name!r} should mount /app/config exactly once; found {config_mounts}"
    is_read_only = config_mounts[0].endswith(":ro")
    if name in CONFIG_WRITERS:
        assert not is_read_only, (
            f"service {name!r} runs cli/apply --commit and cli/recalibrate --commit, "
            "which rewrite settings.yml — it needs a writable config mount"
        )
    else:
        assert is_read_only, (
            f"service {name!r} never writes config (services/settings_rewriter is reached "
            "only from cli/apply and cli/recalibrate) — mount /app/config read-only"
        )
