# Stage 9.0: equity integration evidence and unresolved risk contract

Status: **real securities API integration and dependent workflows explicitly
deferred by the operator on 2026-10-03**. Reentry requires verified official
Kraken Securities stock/ETF API support, not an assumption that no API exists.
The subsequent request adopts a disabled boolean boundary and proportionate
contract-supported infrastructure; ADR-055 governs that current slice. It is
implemented in `config/equities.py`, not a trading adapter. The intended design
target is a cash account; account existence and entitlement remain unverified.
This scope revision removes deferred integration from the current completion
gate without marking it implemented. No support monitor or external contact.

Reviewed 2026-10-03 in the authorized cloud checkout. Historical investigation
and input matrices below remain reentry evidence, not outstanding questions that
must block the revised assignment. No account keys, real orders, new venue,
paid feed or capital allocation were used.

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

## Supplemental official-source evidence supplied by the parent

The parent supplied this bounded read-only research on 2026-10-03. It adds to the
local review above; it is not an authenticated API test or account confirmation.

| Official source | Supported conclusion and limit |
| --- | --- |
| [REST AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs) | Documents `aclass_base` values `currency` and `tokenized_asset` (xStocks); does not establish the committed Securities stock/ETF contract. |
| [Spot WebSocket v2 instrument](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/instrument) | Documents crypto instruments and `include_tokenized_assets` for xStocks; tokenized assets are not evidence of Securities API entitlement. |
| [API Partner Program announcement, July 29, 2026](https://blog.kraken.com/product/api/kraken-api-partner-program-infrastructure) | Lists crypto spot/futures (excluding US CME) and xStocks, without a Securities stock/ETF API contract. An omitted product is not proof of universal API unavailability. |
| [Developer index](https://docs.kraken.com/llms.txt) | Lists Exchange, Institutional and Embed surfaces; it does not establish this user's Securities entitlement. |
| [Getting started with equities](https://support.kraken.com/articles/getting-started-with-equities) | Describes real stock ownership, Kraken Securities LLC and account eligibility. App/Pro access does not imply API access. |
| [xStocks availability](https://support.kraken.com/gb/articles/xstocks-availability) | Excludes US xStocks access; it does not resolve eligibility for the distinct Securities product. |

No verified Kraken Securities stock/ETF API contract was found, and no blanket
explicit statement that such an API is unavailable was found either. Preserve
that distinction. Public sources conflict on EEA stock availability; do not infer
Carl's eligibility, residence, account type or tax status from those pages.
The research used no credentials, private-data transmission or external writes.
The parent has asked Carl for the account/API/jurisdiction facts and the pylint
semantic-check decision. Answers remain pending; no acceptance is inferred.
No further broad research or dependent implementation is needed before those
answers arrive. Existing implemented work and verification receipts are preserved.

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

## Contract inputs versus acceptance access (follow-up review)

Authority: roadmap Phase 9's 2026-09-08 reconciliation explicitly labels the May
sketch assumptions, and the accepted closeout plan §4/G9 requires a fresh risk
ADR before implementation. The committed outcomes are 9.1 securities execution,
9.2 account/settlement-aware safety, 9.3 earnings pauses, 9.5 tax export and web
summaries, and the separately authorized live acceptance in 9.4/9.6. The old
`asset_class` spelling and unconditional PDT counter are not adopted contracts.

| Input | Publicly researchable facts | Only operator/broker can establish | Why this blocks code, or only acceptance |
| --- | --- | --- | --- |
| Securities API | Official endpoint/version, authentication, entitlement documentation; stock identifiers, precision, fractional/whole-share minima; order types/TIF, sessions, errors, fill/cancel/reconciliation schema | Whether the intended Kraken Securities account can use that API, and an official link/support confirmation if the API is private | No documented wire contract means 9.1 cannot map stock orders or persist authoritative fills without inventing an API. Credentials are **not** needed to implement against an official contract; authenticated response samples and real fills are later validation. |
| Account policy | Cash-versus-margin settlement rules, broker agreement and house/day-trading rules, exchange holiday/session calendars | Cash or margin account; country/state of residence; actual broker entity/product eligibility; applicable house/transition policy | Chooses the safety invariant in 9.2: settled cash/reservations versus the applicable margin/day-trading policy. Borrowing stays excluded. Implementing an assumed policy can allow a prohibited counter-order. Actual account balance, starting positions and transaction history are validation/configuration inputs, not prerequisites for policy code. |
| Earnings source | Candidate feed's official future-calendar API, coverage, revision/cancellation semantics, time zones, timestamps, license and rate limits | Any existing entitlement/source the operator wants used; permission only if new paid access is necessary | 9.3 needs an authoritative future-event contract. It does not need a real upcoming event to implement pause-window logic. A free documented source within existing permissions could be selected autonomously; one has not been established here. Do not require a paid choice by default. Once 9.0's safety contract is settled, implement deterministic pause/override/stale-data tests before live feed access. |
| Tax scope and lots | IRS lot-identification, basis/holding-period and wash-sale rules; current export field definitions; broker's documented lot and corporate-action format | US-taxpayer/tax jurisdiction; broker lot-selection method (e.g. FIFO or specific identification); import/export format available; presence of opening/transferred lots and outside-account replacement acquisitions | Jurisdiction and lot-selection semantics affect 9.5 calculations, not just test data. A documented broker format permits offline implementation without private history. Actual lots, corporate actions and external transactions are completeness/reconciliation inputs for final acceptance. Do not claim these private data are needed merely to write an importer or deterministic calculator. Unknown external activity must remain explicit in exports, never treated as absent. |
| Activation | Documented operational runbook and minimum supported order size | Later capital allocation and explicit real-order authorization | Blocks only 9.4/9.6 financial acceptance and deployment. It does not block contract-backed local development. |

The official [AssetPairs reference](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)
was re-read: its `aclass_base` values distinguish `currency` from `tokenized_asset`
(xStocks). That is positive evidence for the inspected Spot surface; it does not
supply a Kraken Securities stock/ETF contract. xStocks are not a substitute for
this product's committed equities track. The failed support-page retrieval is
an access failure, not proof that no securities API exists.

[IRS Publication 550](https://www.irs.gov/publications/p550) establishes that
identified shares use their particular basis, and wash-sale analysis includes
substantially identical acquisitions around a loss sale, including certain
outside-account/related-party acquisitions. These rules support a future local
calculator; they cannot reveal the operator's broker election or opening lots.
No tax result is inferred from the crypto trade table.

Smallest first reply (no secrets or account documents required):

1. Intended Kraken Securities account: **cash or margin**, country/state of
   residence, and US-taxpayer status.
2. **Official securities API documentation link**, or redacted Kraken support
   confirmation identifying the endpoint/product and whether API access is enabled.

If already known, add the authorized earnings source and broker lot-selection /
export format. Those can be resolved later during 9.0; do not burden the first
reply with balances, credentials or full transaction history. With the first two
facts, resume the risk ADR and all contract-supported offline work. Keep equity
activation disabled and retain later private/live acceptance gates separately.
