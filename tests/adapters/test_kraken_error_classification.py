"""ADR-037 — Kraken error classification (permanent-auth vs lockout)."""

from __future__ import annotations

import pytest

from wobblebot.adapters.kraken_exchange import (
    PERMANENT_AUTH_CODES,
    is_permanent_auth_error,
    is_temporary_lockout,
)
from wobblebot.ports.exceptions import ExchangeError

pytestmark = pytest.mark.unit


class TestExchangeErrorCodes:
    def test_codes_attribute_round_trip(self) -> None:
        exc = ExchangeError("Kraken /x returned errors", codes=["EAPI:Invalid key"])
        assert exc.codes == ["EAPI:Invalid key"]

    def test_codes_default_empty(self) -> None:
        assert ExchangeError("plain failure").codes == []


class TestPermanentAuthClassifier:
    @pytest.mark.parametrize("code", sorted(PERMANENT_AUTH_CODES))
    def test_each_permanent_code(self, code: str) -> None:
        assert is_permanent_auth_error(ExchangeError("boom", codes=[code]))

    def test_message_fallback_without_codes(self) -> None:
        """Errors wrapped/re-raised without codes still classify — the
        incident's log lines carried the code only in the message."""
        exc = ExchangeError("Kraken /0/private/BalanceEx returned errors: ['EAPI:Invalid key']")
        assert is_permanent_auth_error(exc)

    @pytest.mark.parametrize(
        "code",
        ["EGeneral:Temporary lockout", "EAPI:Rate limit exceeded", "EService:Unavailable"],
    )
    def test_transient_codes_are_not_permanent(self, code: str) -> None:
        assert not is_permanent_auth_error(ExchangeError("boom", codes=[code]))

    def test_plain_transport_error_is_not_permanent(self) -> None:
        assert not is_permanent_auth_error(ExchangeError("Kraken /x HTTP 502"))


class TestTemporaryLockoutClassifier:
    def test_lockout_code(self) -> None:
        assert is_temporary_lockout(ExchangeError("boom", codes=["EGeneral:Temporary lockout"]))

    def test_lockout_message_fallback(self) -> None:
        assert is_temporary_lockout(
            ExchangeError(
                "Kraken /0/private/OpenOrders returned errors: ['EGeneral:Temporary lockout']"
            )
        )

    def test_invalid_key_is_not_lockout(self) -> None:
        assert not is_temporary_lockout(ExchangeError("boom", codes=["EAPI:Invalid key"]))
