"""Regression: every long-running daemon config has a sensible
log_file_path default.

The 2026-05-25 cli/harvest diagnostic incident took ~30 min to root-cause
because the daemon's stderr was the only postmortem source and the
operator's terminal had no scrollback for the relevant window. The fix
landed file logging across every long-running daemon with a per-daemon
default path. This test pins the defaults so a future config refactor
doesn't accidentally unwire any of them by setting the default back
to None.

Per ``feedback_status_doc_discipline`` and the file-logging audit, the
contract is:

- One-shot tools (status, preflight, sandbox) — no log_file_path field.
- Long-running daemons (live, shadow, observe, news, advise, harvest,
  operator, web, maintenance) — log_file_path defaults to
  ``data/logs/<daemon>.log``.
"""

from __future__ import annotations

import pytest

from wobblebot.config.cli import (
    AdviseConfig,
    HarvestConfig,
    LiveConfig,
    MaintenanceConfig,
    NewsConfig,
    ObserveConfig,
    OperatorConfig,
    PreflightConfig,
    SandboxConfig,
    ShadowConfig,
    StatusConfig,
    WebConfig,
)
from wobblebot.domain.value_objects import Symbol

pytestmark = pytest.mark.unit


_BTC_USD = Symbol(base="BTC", quote="USD")


@pytest.mark.parametrize(
    "model_factory,expected_default",
    [
        (lambda: LiveConfig(symbols=[_BTC_USD]), "data/logs/live.log"),
        (
            lambda: ShadowConfig(
                symbols=[_BTC_USD],
                initial_balances={"USD": 10000},  # type: ignore[arg-type]
            ),
            "data/logs/shadow.log",
        ),
        (lambda: ObserveConfig(symbols=[_BTC_USD]), "data/logs/observe.log"),
        (lambda: NewsConfig(), "data/logs/news.log"),
        (lambda: AdviseConfig(symbols=[_BTC_USD]), "data/logs/advise.log"),
        (lambda: HarvestConfig(), "data/logs/harvest.log"),
        (lambda: MaintenanceConfig(), "data/logs/maintenance.log"),
    ],
)
def test_daemon_config_defaults_log_file_path(model_factory, expected_default) -> None:  # type: ignore[no-untyped-def]
    """Each long-running daemon's config defaults log_file_path to a
    per-daemon path under data/logs/. Operators can override by setting
    a custom value or null to disable file logging."""
    model = model_factory()
    assert getattr(model, "log_file_path", None) == expected_default


def test_operator_config_defaults_log_file_path() -> None:
    """OperatorConfig requires nested auth + assistant blocks; build
    minimal versions for the default-value check."""
    from wobblebot.config.cli import AssistantLLMConfig, OperatorAuthConfig

    model = OperatorConfig(
        auth=OperatorAuthConfig(outbound_channel_id="123"),
        assistant=AssistantLLMConfig(model="phi4:14b-q8_0"),
    )
    assert model.log_file_path == "data/logs/operator.log"


def test_web_config_defaults_log_file_path() -> None:
    """WebConfig has all-optional fields so it constructs with no args."""
    model = WebConfig()
    assert model.log_file_path == "data/logs/web.log"


@pytest.mark.parametrize(
    "model_factory",
    [
        lambda: PreflightConfig(symbol=_BTC_USD),
        lambda: StatusConfig(symbol=_BTC_USD),
        lambda: SandboxConfig(),
    ],
)
def test_oneshot_configs_omit_log_file_path(model_factory) -> None:  # type: ignore[no-untyped-def]
    """One-shot tools (preflight, status, sandbox) don't need file
    logging — the operator runs them interactively and watches stderr.
    Adding the field unnecessarily would just create empty log files."""
    model = model_factory()
    assert not hasattr(model, "log_file_path")


def test_log_file_path_can_be_disabled_per_daemon() -> None:
    """Operator opts out of file logging by setting null in YAML."""
    model = LiveConfig(symbols=[_BTC_USD], log_file_path=None)
    assert model.log_file_path is None
