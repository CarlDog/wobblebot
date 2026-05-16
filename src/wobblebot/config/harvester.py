"""HarvesterConfig — Phase 4 treasury-management thresholds.

Decides when the Harvester proposes a Kraken→bank withdrawal
(surplus scrape) or a bank→Kraken deposit (top-up). Per ADR-003 +
ADR-004, the actual transfer goes through Kraken's withdrawal API via
``ExchangePort``; the config below holds only the *rules*, not the
mechanism.

Threshold semantics (Stage 4.1, USD denominated for v1):

- ``min_exchange_liquidity_usd``: floor on Kraken balance. The
  Harvester refuses to propose a withdrawal that would push balance
  below this — the engine needs liquidity to keep trading.
- ``surplus_threshold_usd``: ceiling on Kraken balance. Above this,
  the Harvester proposes scraping the excess to bank, leaving
  ``min_exchange_liquidity_usd`` as the post-scrape floor target.
- ``topup_threshold_usd``: low-water mark *above* the floor. Between
  floor and topup the Harvester proposes a bank→Kraken deposit
  bringing balance back toward ``surplus_threshold_usd`` minus a
  small buffer (so we don't immediately scrape what we just deposited).
- ``max_withdrawal_per_day_usd``: hard ceiling on cumulative
  withdrawals (exchange→bank) over a rolling 24h window. Limits the
  blast radius of a misconfiguration or a runaway Harvester loop.

Invariant: ``min_exchange_liquidity_usd < topup_threshold_usd < surplus_threshold_usd``.
Enforced by a model validator below; misconfiguration fails fast at
config-load time rather than at first balance check.

The ``enabled`` flag defaults to ``False`` mirroring the auto-apply
gate posture (ADR-012 spirit): operator-opt-in for anything that
moves money. Phase 4.2 (read-only balance monitoring) ignores the
flag; Phase 4.3+ honor it.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, model_validator


class HarvesterConfig(BaseModel):
    """Operator-tunable thresholds for the Harvester loop.

    All amounts in USD. Per-asset configs (BTC scraping, ETH
    scraping, etc.) deferred to a later stage when the operator
    actually wants more than fiat sweep coverage.
    """

    enabled: bool = False
    min_exchange_liquidity_usd: Decimal = Field(gt=Decimal("0"))
    surplus_threshold_usd: Decimal = Field(gt=Decimal("0"))
    topup_threshold_usd: Decimal = Field(gt=Decimal("0"))
    max_withdrawal_per_day_usd: Decimal = Field(gt=Decimal("0"))

    class Config:
        frozen = True

    @model_validator(mode="after")
    def _validate_threshold_ordering(self) -> HarvesterConfig:
        """Enforce ``min < topup < surplus``.

        If the operator inverts these the decision logic produces
        nonsense (e.g. balance is "both above surplus AND below topup"
        when topup > surplus). Fail at config-load so the error
        surfaces with a useful message instead of a confused proposal
        at runtime.
        """
        if not self.min_exchange_liquidity_usd < self.topup_threshold_usd:
            raise ValueError(
                f"min_exchange_liquidity_usd ({self.min_exchange_liquidity_usd}) "
                f"must be < topup_threshold_usd ({self.topup_threshold_usd})"
            )
        if not self.topup_threshold_usd < self.surplus_threshold_usd:
            raise ValueError(
                f"topup_threshold_usd ({self.topup_threshold_usd}) "
                f"must be < surplus_threshold_usd ({self.surplus_threshold_usd})"
            )
        return self
