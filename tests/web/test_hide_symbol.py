"""Dashboard card visibility — the eye toggle (Group 3).

Two properties carry the whole feature.

**It cannot move a number.** The hidden set is applied to the card
loop's iterable and to nothing else. ``snapshot.symbols`` stays whole, so
the price fetch, sparklines, account value, realized P&L and the fills
tables are byte-identical whether a card is hidden or not. A view
preference that changed reported P&L is the worst outcome this feature
could produce, and the tests below assert it is impossible rather than
merely unlikely.

**It cannot hide a control.** Pause, resume and re-anchor live inside the
card, so hiding a symbol the engine trades would silently un-ship them.
Hiding is therefore restricted to symbols outside ``live.symbols``,
enforced in the route and not only in the template.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from tests.web._helpers import TEST_PASSWORD, TEST_USERNAME, csrf_from, login_as
from tests.web.test_status import _build_client, _make_order
from wobblebot.adapters.sqlite_storage import SQLiteStorageAdapter
from wobblebot.domain.value_objects import Symbol
from wobblebot.web.auth import hash_password

pytestmark = pytest.mark.unit

_BTC = Symbol(base="BTC", quote="USD")
_BABY = Symbol(base="BABY", quote="USD")


def _snapshot(**kwargs: object):  # type: ignore[no-untyped-def]
    from wobblebot.web.routes.status import StatusSnapshot

    base: dict[str, object] = {
        "live_wired": True,
        "open_orders": (),
        "recent_trades": (),
        "last_fill_age_seconds": None,
    }
    base.update(kwargs)
    return StatusSnapshot(**base)  # type: ignore[arg-type]


class TestHiddenSetIsAppliedOnlyToTheCardLoop:
    """Pinned against the snapshot rather than the DOM: the invariant is
    that the hidden set never reaches the fields money is derived from."""

    def test_the_snapshot_keeps_every_symbol(self) -> None:
        snap = _snapshot(symbols=(_BTC, _BABY), hidden_symbols=frozenset({_BABY}))
        # The union is untouched — only the template skips.
        assert snap.symbols == (_BTC, _BABY)
        assert _BABY in snap.symbols
        assert snap.hidden_symbols == frozenset({_BABY})

    def test_hidden_symbols_defaults_to_empty(self) -> None:
        """Every existing construction site keeps working, and an operator
        who has hidden nothing pays no behavior change."""
        assert _snapshot().hidden_symbols == frozenset()


@pytest.mark.asyncio
class TestLoadHiddenSymbols:
    async def test_unwired_storage_hides_nothing(self) -> None:
        from wobblebot.web.routes.status import _load_hidden_symbols

        assert await _load_hidden_symbols(None, 1) == frozenset()

    async def test_no_user_hides_nothing(self) -> None:
        from wobblebot.web.routes.status import _load_hidden_symbols

        assert await _load_hidden_symbols(None, None) == frozenset()

    async def test_a_storage_failure_shows_every_card(self) -> None:
        """The safe direction. A hide feature that fails by hiding things
        is worse than one that fails by showing them."""
        from wobblebot.ports.exceptions import StorageError
        from wobblebot.web.routes.status import _load_hidden_symbols

        class _Boom:
            async def get_hidden_symbols(self, user_id: int) -> frozenset[Symbol]:
                raise StorageError("nope")

        assert await _load_hidden_symbols(_Boom(), 1) == frozenset()  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def operator_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    await adapter.create_user(TEST_USERNAME, hash_password(TEST_PASSWORD, cost=10))
    yield adapter
    await adapter.close()


@pytest_asyncio.fixture
async def live_storage() -> AsyncIterator[SQLiteStorageAdapter]:
    adapter = SQLiteStorageAdapter(":memory:")
    await adapter.connect()
    yield adapter
    await adapter.close()


def _post(client: TestClient, path: str, **form: str):  # type: ignore[no-untyped-def]
    """POST a CSRF-protected form the way the dashboard's own forms do."""
    token = csrf_from(client.get("/dashboard").text)
    return client.post(path, data={"csrf_token": token, **form})


@pytest.mark.asyncio
class TestHideEndToEnd:
    """Through the real routes and the real template.

    BTC is configured and traded; BABY is neither — a card from a dust
    balance, which is the case the operator asked about.
    """

    async def _seed(self, live_storage: SQLiteStorageAdapter) -> None:
        await live_storage.save_order(_make_order(symbol="BTC/USD"))
        await live_storage.save_order(_make_order(symbol="BABY/USD", price="0.0114"))

    async def test_hiding_removes_the_card_and_names_it_in_the_summary(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await self._seed(live_storage)
        with _build_client(operator_storage, live_storage, live_symbols=(_BTC,)) as client:
            login_as(client)
            before = client.get("/dashboard").text
            assert "BABY/USD" in before
            assert "hidden-summary" not in before

            resp = _post(client, "/commands/hide-symbol", symbol="BABY/USD", hidden="true")
            assert resp.status_code == 303

            after = client.get("/dashboard").text
            assert "hidden-summary" in after
            assert "1 symbol hidden" in after
            assert "BTC/USD" in after  # the traded card is untouched

    async def test_a_hidden_symbol_can_be_revealed(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await self._seed(live_storage)
        with _build_client(operator_storage, live_storage, live_symbols=(_BTC,)) as client:
            login_as(client)
            _post(client, "/commands/hide-symbol", symbol="BABY/USD", hidden="true")
            assert "hidden-summary" in client.get("/dashboard").text
            _post(client, "/commands/hide-symbol", symbol="BABY/USD", hidden="false")
            assert "hidden-summary" not in client.get("/dashboard").text

    async def test_a_hidden_symbol_with_resting_orders_is_called_out(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """The safety half. Hiding is restricted to symbols the engine does
        NOT trade — exactly the ones cli/live never ticks and clean shutdown
        never cancels. Hiding the card must not also hide the fact that
        capital is resting there unmanaged."""
        await self._seed(live_storage)
        with _build_client(operator_storage, live_storage, live_symbols=(_BTC,)) as client:
            login_as(client)
            _post(client, "/commands/hide-symbol", symbol="BABY/USD", hidden="true")
            body = client.get("/dashboard").text
            assert "hidden-symbol-warn" in body
            assert "nothing will cancel it" in body

    async def test_a_configured_symbol_is_refused_by_the_route(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """Enforced server-side, not only by omitting the button: pause,
        resume and re-anchor live inside the card, so hiding a traded
        symbol would leave no surface to stop it."""
        await self._seed(live_storage)
        with _build_client(operator_storage, live_storage, live_symbols=(_BTC,)) as client:
            login_as(client)
            resp = _post(client, "/commands/hide-symbol", symbol="BTC/USD", hidden="true")
            assert resp.status_code == 400
            assert "cannot be hidden" in resp.text
            body = client.get("/dashboard").text
            assert "hidden-summary" not in body
            assert "BTC/USD" in body

    async def test_the_eye_renders_only_for_an_untraded_symbol(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        await self._seed(live_storage)
        with _build_client(operator_storage, live_storage, live_symbols=(_BTC,)) as client:
            login_as(client)
            body = client.get("/dashboard").text
            assert 'aria-label="Hide BABY/USD"' in body
            assert 'aria-label="Hide BTC/USD"' not in body

    async def test_revealing_a_configured_symbol_is_allowed(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """The guard is on HIDING only. A symbol hidden while untraded and
        since added to live.symbols must still be revealable — otherwise a
        config change strands a card permanently out of sight."""
        await self._seed(live_storage)
        with _build_client(operator_storage, live_storage, live_symbols=(_BTC,)) as client:
            login_as(client)
            _post(client, "/commands/hide-symbol", symbol="BABY/USD", hidden="true")
        # BABY joins the trading set on the next deploy.
        with _build_client(operator_storage, live_storage, live_symbols=(_BTC, _BABY)) as client:
            login_as(client)
            resp = _post(client, "/commands/hide-symbol", symbol="BABY/USD", hidden="false")
            assert resp.status_code == 303
            assert "hidden-summary" not in client.get("/dashboard").text

    async def test_the_summary_container_can_legally_hold_a_form(
        self, operator_storage: SQLiteStorageAdapter, live_storage: SQLiteStorageAdapter
    ) -> None:
        """The one thing substring assertions structurally cannot see.

        The first cut wrapped this in a <p>. <form> is flow content and
        <p> accepts only phrasing content, so a browser's HTML5 parser
        CLOSES the paragraph at the first <form>: the container renders
        empty, the forms land as siblings, and every rule scoped under
        .hidden-summary silently stops applying. Every other assertion in
        this file still passed. Rendering it in a real browser is what
        exposed it.

        Asserted on the tag rather than by re-parsing, deliberately.
        Python's html.parser reports tags literally and applies none of
        HTML5's implied-end-tag rules, so a nesting check written with it
        passes against the broken markup too — verified by mutation, which
        is why that version of this test was thrown away. No HTML5 parser
        is a dependency here, and adding one to assert a fixed tag name
        would be the expensive way to say the same thing.
        """
        await self._seed(live_storage)
        with _build_client(operator_storage, live_storage, live_symbols=(_BTC,)) as client:
            login_as(client)
            _post(client, "/commands/hide-symbol", symbol="BABY/USD", hidden="true")
            body = client.get("/dashboard").text

        assert '<div class="hidden-summary">' in body
        assert '<p class="hidden-summary">' not in body
