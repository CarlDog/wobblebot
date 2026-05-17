"""Tests for OperatorConfig schema (Stage 5.6.B)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from wobblebot.config.cli import (
    AssistantLLMConfig,
    OperatorAuthConfig,
    OperatorConfig,
)

pytestmark = pytest.mark.unit


def _auth() -> OperatorAuthConfig:
    return OperatorAuthConfig(
        allowed_user_ids=frozenset({"42"}),
        allowed_channel_ids=frozenset({"100"}),
        outbound_channel_id="100",
    )


def _assistant() -> AssistantLLMConfig:
    return AssistantLLMConfig(model="phi4:14b")


# --------------------------------------------------------------------- #
# AssistantLLMConfig                                                    #
# --------------------------------------------------------------------- #


class TestAssistantLLMConfig:
    def test_defaults(self) -> None:
        cfg = AssistantLLMConfig(model="phi4:14b")
        assert cfg.provider == "ollama"
        assert cfg.prompt_file == "config/prompts/operator.md"
        assert cfg.base_url == "http://localhost:11434"
        assert cfg.temperature == 0.3
        assert cfg.max_tokens == 512

    def test_temperature_out_of_range_raises(self) -> None:
        with pytest.raises(ValidationError):
            AssistantLLMConfig(model="x", temperature=5.0)

    def test_max_tokens_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            AssistantLLMConfig(model="x", max_tokens=0)

    def test_model_required_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            AssistantLLMConfig(model="")

    def test_frozen(self) -> None:
        cfg = AssistantLLMConfig(model="x")
        with pytest.raises(ValidationError):
            cfg.temperature = 0.5  # type: ignore[misc]


# --------------------------------------------------------------------- #
# OperatorAuthConfig                                                    #
# --------------------------------------------------------------------- #


class TestOperatorAuthConfig:
    def test_construct_with_allowlists(self) -> None:
        cfg = _auth()
        assert cfg.allowed_user_ids == frozenset({"42"})
        assert cfg.outbound_channel_id == "100"

    def test_outbound_channel_required(self) -> None:
        with pytest.raises(ValidationError):
            OperatorAuthConfig(
                allowed_user_ids=frozenset(),
                allowed_channel_ids=frozenset(),
                outbound_channel_id="",
            )

    def test_default_token_env_var(self) -> None:
        cfg = _auth()
        assert cfg.bot_token_env_var == "DISCORD_BOT_TOKEN"

    def test_frozen(self) -> None:
        cfg = _auth()
        with pytest.raises(ValidationError):
            cfg.outbound_channel_id = "999"  # type: ignore[misc]


# --------------------------------------------------------------------- #
# OperatorConfig                                                        #
# --------------------------------------------------------------------- #


class TestOperatorConfig:
    def test_construct(self) -> None:
        cfg = OperatorConfig(auth=_auth(), assistant=_assistant())
        assert cfg.operator_db == "data/wobblebot-operator.db"
        assert cfg.context_window_turns == 10
        assert cfg.confirm_ttl_seconds == 300
        assert cfg.forwarder_poll_seconds == 2.0
        assert cfg.live_db is None
        assert cfg.advise_db is None
        assert cfg.news_db is None
        assert cfg.harvest_db is None

    def test_cross_db_paths_optional(self) -> None:
        cfg = OperatorConfig(
            auth=_auth(),
            assistant=_assistant(),
            live_db="data/live.db",
            advise_db="data/advise.db",
            news_db="data/news.db",
            harvest_db="data/harvest.db",
        )
        assert cfg.live_db == "data/live.db"
        assert cfg.advise_db == "data/advise.db"
        assert cfg.news_db == "data/news.db"
        assert cfg.harvest_db == "data/harvest.db"

    def test_context_window_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            OperatorConfig(auth=_auth(), assistant=_assistant(), context_window_turns=0)

    def test_context_window_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            OperatorConfig(auth=_auth(), assistant=_assistant(), context_window_turns=100)

    def test_confirm_ttl_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            OperatorConfig(auth=_auth(), assistant=_assistant(), confirm_ttl_seconds=0)

    def test_forwarder_poll_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            OperatorConfig(auth=_auth(), assistant=_assistant(), forwarder_poll_seconds=0)

    def test_frozen(self) -> None:
        cfg = OperatorConfig(auth=_auth(), assistant=_assistant())
        with pytest.raises(ValidationError):
            cfg.operator_db = "other.db"  # type: ignore[misc]

    def test_auth_required(self) -> None:
        with pytest.raises(ValidationError):
            OperatorConfig(assistant=_assistant())  # type: ignore[call-arg]

    def test_assistant_required(self) -> None:
        with pytest.raises(ValidationError):
            OperatorConfig(auth=_auth())  # type: ignore[call-arg]
