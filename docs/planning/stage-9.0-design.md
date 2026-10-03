# Stage 9.0: equity integration evidence and unresolved risk contract

Status: kickoff investigation, **not ratified and not implemented**. This preserves
the committed Phase 9 track; it does not replace the roadmap with a smaller product.
Reviewed 2026-10-03 in the authorized cloud checkout. No account keys, real orders,
new venue, paid feed or capital allocation were used.

## Official evidence and effect on the old sketch

- [Kraken AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)
  and [AddOrder](https://docs.kraken.com/api-reference/trading/add-order) were read.
  The public pages inspected do not establish the May sketch's equities
  `asset_class` contract. The [developer index](https://docs.kraken.com/llms.txt)
  identifies Exchange, Institutional and Embed surfaces; it does not establish
  account-enabled stock execution through our current Spot adapter. This is an
  evidence gap, not a claim that Kraken cannot offer an equities API.
- [FINRA's transition guidance](https://www.finra.org/investors/insights/intraday-margin-requirements)
  says new intraday margin requirements became effective June 4, 2026, with a
  broker transition through October 20, 2027. A broker may still use the old PDT
  regime during that window. Therefore the historical unconditional 3-in-5-day /
  $25,000 assumption cannot determine our account policy.
- [FINRA's cash-account guidance](https://www.finra.org/investors/insights/frequent-intraday-trading)
  identifies T+1 settlement and cash-account good-faith/free-riding constraints.
  Margin-account and cash-account rules differ; no account type is inferred from
  a small balance or from Kraken's crypto account.

Public Kraken contract integration remains blocked by the execution environment's
previously recorded proxy 403/direct DNS failures. Several Kraken support URLs
were inaccessible through the research tool. Search attempts returned unrelated
results even with domain filters; these are not evidence. No repeated search is
needed until an official account/API source is available.

## Smallest required input

Confirm the intended Kraken Securities account type, jurisdiction and official
account-enabled equities API contract (or Kraken support confirmation). If margin,
provide the broker's applicable transition/house rules; borrowing remains outside
scope. Recommendation: retain default-disabled equities until the real account
and contract establish the safety model. The question was sent while independent
N4/N5 implementation continued. No answer or elapsed time is treated as approval.

## Decisions already bounded by existing scope

1. Keep Kraken as the sole venue; do not substitute an Alpaca direct account.
2. Keep financial effects in the deterministic engine and human approval path.
3. Keep equity activation off and refuse unsupported instruments; never reinterpret
   a stock as a crypto ticker or tokenized proxy.
4. Require exact symbol, precision, lot/minimum, order-type, session and error
   contracts before adding adapter mappings.
5. Settlement uses the actual broker calendar and settled buying power; calendar
   days and apparent total USD are not a sufficient substitute.
6. Persist/reconcile fills and reservations before restarting equity work; unknown
   prior positions or unsettled funds must block placement.
7. Derive applicable day-trading rules from confirmed account/house policy, not
   the superseded historical sketch or assumed use of margin.
8. Earnings data must have provenance, freshness and an explicit unavailable-data
   pause posture. EDGAR filings alone do not establish a future earnings calendar;
   a licensed source and supported symbols remain unresolved.
9. Tax export must reconcile broker lots, corporate actions and outside-account
   wash-sale inputs before claiming tax correctness. The existing crypto trades
   table alone is insufficient. Filing/compliance acceptance is not invented.
10. Live tiny-cycle and multi-symbol acceptance remain separately authorized
    financial actions after offline development gates pass.

## Completion gates retained

9.1 requires official/account-tested API fixtures. 9.2 requires the ratified account,
settlement/calendar and applicable day-trading contract. 9.3 requires the earnings
source. 9.5 requires broker lot/corporate-action/tax scope. 9.4 and 9.6 retain real
capital/event/account verification. No placeholder adapter, guessed PDT counter,
or synthetic tax export is presented as completion. Allocate the next unused
ADR number when the material inputs can be ratified; ADR-019 is not reused.
