"""Kraken Ledgers ingest (ADR-040 follow-up).

Test seam: httpx.MockTransport, same as the sibling adapter tests.

The payload shapes below are Kraken's real ones, captured from the live
account on 2026-08-22 while diagnosing why SOL's replayed quantity
disagreed with its balance.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from wobblebot.adapters.kraken_exchange import KrakenAdapter
from wobblebot.config.kraken import KrakenConfig
from wobblebot.ports.exceptions import ExchangeError

pytestmark = pytest.mark.unit

_TEST_SECRET = "c2VjcmV0"  # base64("secret")

_ASSETS = {
    "error": [],
    "result": {
        "XETH": {"altname": "ETH"},
        "SOL": {"altname": "SOL"},
        "ZUSD": {"altname": "USD"},
    },
}


def _adapter(routes: dict[str, dict[str, Any]]) -> KrakenAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"error": [], "result": {}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.kraken.com", transport=transport)
    return KrakenAdapter(
        config=KrakenConfig(api_key="k", api_secret=_TEST_SECRET), http_client=client
    )


def _ledger_payload(ledger: dict[str, Any], count: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ledger": ledger}
    if count is not None:
        result["count"] = count
    return {"error": [], "result": result}


_REAL_STAKING_ROW = {
    "refid": "ELXLVMZ-NMXU7-JX562L",
    "time": 1787394251.69978,
    "type": "staking",
    "asset": "SOL",
    "amount": "0.0000688812",
    "fee": "0.0000206643",
}


@pytest.mark.asyncio
class TestLedgerParsing:
    async def test_parses_a_real_staking_row(self) -> None:
        adapter = _adapter(
            {
                "/0/public/Assets": _ASSETS,
                "/0/private/Ledgers": _ledger_payload({"L1": _REAL_STAKING_ROW}, count=1),
            }
        )
        entries = await adapter.get_ledger_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.id == "L1"
        assert entry.entry_type == "staking"
        assert entry.amount == Decimal("0.0000688812")
        assert entry.fee == Decimal("0.0000206643")
        # The number the balance actually moved by — the distinction
        # that stalled the 2026-08-22 diagnosis until it was applied.
        assert entry.net_amount == Decimal("0.0000482169")

    async def test_asset_code_is_normalized_to_internal(self) -> None:
        """Kraken says XETH; every other port method says ETH. Callers
        must never have to know the exchange's naming."""
        row = dict(_REAL_STAKING_ROW, asset="XETH")
        adapter = _adapter(
            {
                "/0/public/Assets": _ASSETS,
                "/0/private/Ledgers": _ledger_payload({"L1": row}, count=1),
            }
        )
        (entry,) = await adapter.get_ledger_entries()
        assert entry.asset == "ETH"

    async def test_unknown_type_survives_verbatim(self) -> None:
        """No mapping onto a closed enum — a reward type added later
        must arrive as itself, not be dropped or misfiled."""
        row = dict(_REAL_STAKING_ROW, type="some_future_reward")
        adapter = _adapter(
            {
                "/0/public/Assets": _ASSETS,
                "/0/private/Ledgers": _ledger_payload({"L1": row}, count=1),
            }
        )
        (entry,) = await adapter.get_ledger_entries()
        assert entry.entry_type == "some_future_reward"

    async def test_malformed_row_raises_rather_than_skipping(self) -> None:
        """Loud beats silent: this module has no logger, so skipping a
        bad row would drop income with no trace. The next cycle retries
        and the id-keyed upsert loses nothing."""
        adapter = _adapter(
            {
                "/0/public/Assets": _ASSETS,
                "/0/private/Ledgers": _ledger_payload(
                    {"L1": dict(_REAL_STAKING_ROW, amount="not-a-number")}, count=1
                ),
            }
        )
        with pytest.raises(ExchangeError, match="malformed"):
            await adapter.get_ledger_entries()

    async def test_client_side_asset_filter(self) -> None:
        adapter = _adapter(
            {
                "/0/public/Assets": _ASSETS,
                "/0/private/Ledgers": _ledger_payload(
                    {
                        "L1": _REAL_STAKING_ROW,
                        "L2": dict(_REAL_STAKING_ROW, asset="XETH", refid="R2"),
                    },
                    count=2,
                ),
            }
        )
        assert len(await adapter.get_ledger_entries()) == 2
        assert len(await adapter.get_ledger_entries(asset="ETH")) == 1

    async def test_empty_ledger_is_not_an_error(self) -> None:
        adapter = _adapter(
            {
                "/0/public/Assets": _ASSETS,
                "/0/private/Ledgers": _ledger_payload({}, count=0),
            }
        )
        assert await adapter.get_ledger_entries() == []

    async def test_nonfinite_count_does_not_crash_pagination(self) -> None:
        """json.loads accepts NaN/Infinity and int() on either raises.
        The walk must degrade to the page cap, not explode.

        Served as RAW BYTES: httpx refuses to encode NaN via json=, so
        the only way to reproduce what Kraken could actually put on the
        wire is to write the literal.
        """
        import json as _json

        raw = (
            b'{"error": [], "result": {"ledger": {"L1": '
            + _json.dumps(_REAL_STAKING_ROW).encode()
            + b'}, "count": NaN}}'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/0/public/Assets"):
                return httpx.Response(200, json=_ASSETS)
            return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

        client = httpx.AsyncClient(
            base_url="https://api.kraken.com", transport=httpx.MockTransport(handler)
        )
        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="k", api_secret=_TEST_SECRET), http_client=client
        )
        entries = await adapter.get_ledger_entries(limit=1)
        assert len(entries) == 1


@pytest.mark.asyncio
class TestPagination:
    async def test_pages_are_paced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kraken's call counter is account-wide and a full ledger walk
        alone exceeds it.

        Production, 2026-08-23: an unpaced walk over ~410 entries (9
        pages x 2 points against a 15-point ceiling) returned
        ``EAPI:Rate limit exceeded``. The lesson was already written
        down in tools/reconcile_trade_history.py and this method still
        shipped without it, so it gets a test rather than a comment.
        """
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("wobblebot.adapters.kraken_exchange.asyncio.sleep", fake_sleep)

        pages = [
            {f"L{p}{i}": dict(_REAL_STAKING_ROW, refid=f"R{p}{i}") for i in range(50)}
            for p in range(3)
        ]
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/0/public/Assets"):
                return httpx.Response(200, json=_ASSETS)
            idx = min(calls["n"], len(pages) - 1)
            calls["n"] += 1
            return httpx.Response(200, json=_ledger_payload(pages[idx], count=150))

        client = httpx.AsyncClient(
            base_url="https://api.kraken.com", transport=httpx.MockTransport(handler)
        )
        adapter = KrakenAdapter(
            config=KrakenConfig(api_key="pacing", api_secret=_TEST_SECRET), http_client=client
        )
        try:
            await adapter.get_ledger_entries(limit=1000)
        finally:
            await adapter.aclose()

        # Three pages -> two inter-page waits. Never before the first
        # call, so a single-page account pays nothing.
        assert len(sleeps) == 2, f"expected 2 inter-page delays, got {len(sleeps)}"
        assert all(d >= 2.0 for d in sleeps), sleeps

    async def test_single_page_pays_no_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("wobblebot.adapters.kraken_exchange.asyncio.sleep", fake_sleep)
        adapter = _adapter(
            {
                "/0/public/Assets": _ASSETS,
                "/0/private/Ledgers": _ledger_payload({"L1": _REAL_STAKING_ROW}, count=1),
            }
        )
        try:
            await adapter.get_ledger_entries()
        finally:
            await adapter.aclose()
        assert sleeps == []
