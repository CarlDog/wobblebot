# Backlog index

This is the execution index for the accepted
[2.0.x closeout sequence](2.0-closeout-and-2.1-entry-plan.md). Completion dates,
verification receipts and current phase live only in [the roadmap](roadmap.md).
GitHub remains authoritative for issue and PR state. The legacy tracker items
listed below were re-read on 2026-09-08 UTC and left unchanged by this batch.
The roadmap records the separate stabilization publication PR and its release.

An eligible trigger permits a scope decision; it does not schedule implementation.
Owners below are accountable roles, not claims that another person accepted a task.
The operator assigns the implementation owner when a slice starts. E01-E03 were
accepted on 2026-09-08; publication and NAS deployment were subsequently authorized
and are recorded in the roadmap. Formal acceptance retains its closeout-plan decision.

## Closeout and tracker disposition

| ID | Disposition | Owner / next trigger | Acceptance evidence |
| --- | --- | --- | --- |
| C0 | Scope, branch and WIP preservation completed locally. | Closeout integrator; preserve provenance through release. | Roadmap C0 receipt and local baseline manifest. |
| C1 | Starvation reporting completed locally. | Closeout integrator; C5 deployment and acceptance gates. | Engine/storage/rendered-card tests, old-writer compatibility, mutations and visual inspection in roadmap. |
| C2.1-C2.4 | Four maintenance repairs completed locally. | Closeout integrator; operator decisions and C5. | Production task wiring, corrupt-command containment, loop failure logs and confirmation transport regressions; roadmap receipts. |
| C3/C4 | Evidence prepared; two additional startup repairs and a stale integration fixture corrected. | Closeout integrator + operator; E01-E03 accepted; remaining C5 deployment and acceptance gates. | [Audit and decision packet](2.0-closeout-audit.md); no blanket conformance or phase-close claim. |
| GH18 | Keep [#18](https://github.com/CarlDog/wobblebot/issues/18) open. Mechanical repairs complete; Ruff, ledger-location and CI-shape exceptions accepted for the 2.0.x patch. | Operator + tooling owner; retain accepted exceptions and meet their N0/N2 adoption triggers. | Individual standard rows in audit; fresh machine result plus manual checks. |
| GH22 | Keep [#22](https://github.com/CarlDog/wobblebot/issues/22) as standing model-watch state. | Model-review owner; N5, with separately authorized Fleet Kit changes. | Per-seat coverage including Atlas, current seat register, all G4 prerequisites; no automatic reseating. |
| GH23 | Recommend closing [#23](https://github.com/CarlDog/wobblebot/issues/23) as superseded by later seat evidence, subject to explicit operator approval. | Model-review owner; explicitly retire the incomplete July Anthropic quant campaign, or retain it under G4; later news/risk runs do not complete that campaign. | July campaign comments and August [seat register](../reference/advisor-seats.md); preserve historical probe caveats, no paid rerun for administrative closure. |
| GH97 | Keep [#97](https://github.com/CarlDog/wobblebot/issues/97) open. Public shape checks refreshed; private documentation reviewed, live private behavior unverified. | Provider-maintenance owner; N5 plus the legacy funding decision before funding maintenance. | [API reference](../reference/kraken-api-reference.md), funding migration/retention decision and deduplicated watcher ownership. |
| PR138 | [Pylint bump](https://github.com/CarlDog/wobblebot/pull/138) reviewed; individual CI and local tooling check pass. Merge pending instruction. | Tooling owner; authorized merge, rebase/retest final head if it changes. | Exact head and combined compatibility in audit; source/image publication stays separate. |
| PR139 | [PyYAML stubs bump](https://github.com/CarlDog/wobblebot/pull/139) reviewed; individual CI and local mypy check pass. Merge pending instruction. | Tooling owner; same head/retest gate as PR138. | Exact head and combined compatibility in audit. |
| PR140 | Park [isort major](https://github.com/CarlDog/wobblebot/pull/140). Current CI cannot resolve with pylint 4.0.7; pylint 4.0.8 permits it. Windows default-encoding checks also warn while returning success. | Tooling owner; PR138 first, then explicitly adopt UTF-8 verification and obtain clean Windows/Linux checks. | Isolated isort 9 check passes with UTF-8 and warnings-as-errors; default mode is not accepted. No bulk reformat or environment switch in this patch. |
| C5 | Publication and NAS deployment receipts recorded in the roadmap; formal close remains gated. | Operator + release integrator; authorize the proposed observation window and record acceptance of the remaining limitations. | Verified source/tag/image, preserved stack, current verified backups, rollback rehearsal and bounded behavior receipt; explicit phase close remains outstanding. |
| C5-R1 | Ordinary failed-open shutdown repaired, published and deployed; historical v2.0.8 evidence remains unchanged. | Release integrator; implementation/publication/deployment complete within C5. | Deterministic real-worker regressions, independent review/mutations, supported-version and full Windows/Linux gates, plus [deployment evidence](../release/2.0.9-deployment-evidence.json). |

## Proposed next phase

All five slices wait for formal 2.0.x closure. The detailed contracts and ordering
are in the [accepted sequencing plan](2.0-closeout-and-2.1-entry-plan.md#5-reconciled-21-proposal).
New ADRs use semantic names until an unused number is allocated from the current register.

| ID | Scope / dependency | Owner | Acceptance |
| --- | --- | --- | --- |
| N1 | Residual per-service mount/config/credential isolation; extend accepted ADR-041. | Deployment integrator | Legitimate readers/writers, WAL, backups and settings paths work in isolated Compose; denied capabilities fail; web health no longer requires excess cloud keys. |
| N2 | Reproducible dependency/runtime identity after N1. | Build/tooling integrator | Hashed pip resolution, base/image digests, Python 3.13/3.14 and platform gates, sanitized boot identity, explicit standards/encoding decisions; reconcile CodeQL baseline/configuration before claiming completed analysis. |
| N3 | Atomic command lifecycle, then notification outbox and independent delivery. | Lifecycle integrator | Immutable approvals, one claim per command, post-effect ambiguity/reconciliation, bounded retries and visible failure; no exactly-once Discord promise. Coordinate G1 first. |
| N4 | Independent health response and read-only doctor after N1-N3. | Operations integrator | Dead/wedged operator cannot silence its own alert; unknown/stale states visible. Live, harvest and tools remain page-only, never automatically restarted. |
| N5 | Ollama correctness, then provider contracts/pricing/model-watch ownership. | Provider integrator | Synthetic envelope/error/truncation/cost/privacy tests; deduplicated watch receipts. No paid probe, schedule or model/pricing change without its own scope. Include GH97 legacy funding design. |

## Gated work

| Gate | Retained work | Owner / exact trigger | Acceptance |
| --- | --- | --- | --- |
| G1 | Anomaly detection and disk awareness. | Operations designer; usable per-signal baseline plus accepted consumer/design. Thirty calendar days alone do not clear it. | Coverage/gap/retention analysis per input; calibrate observational baselines and planned gaps, no invented cancellation history from upserts. Disk signal has an agreed consumer. Snapshot findings in roadmap. |
| G2 | Advisor Apply/Approve-Reject; cloud summaries. | Advisor designer; ADR-034 settings-writer ownership and a concrete cloud-summary consumer. | Approval firewall, correct hot-reload expectations and cost-gated summaries; no implied auto-tune daemon. |
| G3 | P4.6 Historian and canonical outcome operations. | Advisor designer; Q2 dump availability, verified imports, canonical NAS scoring, then accepted Historian design. | Coverage and pending/bars-missing/unscoreable tallies; comparisons account for router/selection bias. Rank/hit-rate are not dollar profit. No scored rows in inspected backup. |
| G4 | All 13 [probe-battery findings](../reference/probe-battery-punch-list.md). | Model-review owner; before any new paid seat campaign. | Availability separate from judgment; cap exceptions/order dependence, truncation, effective temperature, deadband/numerics/echo/extraction, contested fixtures, fabricated directions, provenance and ledger roles repaired and verified. |
| G5 | Sell-only offside-high extension. | Operator + strategy designer; proposed ADR-042 ratified only after six-symbol trade-and-ledger reconciliation, retirement policy and net-margin choice. | Multi-episode replay, offside-low places nothing, costs/partial fills/restart semantics, explicit default-off implementation/activation. Daily `clean=6` is insufficient. |
| G6 | Writable POLICY tier and capital-allocation policy. | Configuration/strategy owner; ADR-040 Stage 2 waits for the second qualifying manual POLICY edit and a refreshed fixture. | Accepted policy boundaries, validation, audit and operator-controlled settings. [Proposed ADR-044](../architecture/adr-044-settings-layout-and-policy-authority.md) reviews field ownership, layout, failure semantics and the actual-counter-notional prerequisite; it does not open this gate. Existing capital reports are not automatic envelope growth. |
| G7 | Auto-tune; news auto-pause; confidence extension; learning. | Advisor designer + operator; separate item-specific ADRs and credible P4 outcomes. | Auto-tune needs demonstrated trust/use case; auto-pause needs ADR-002 exception and calibrated false positives; extension needs regime evidence/budget; learning needs 60-90 days of outcomes. |
| G8 | Regime/Oracle, adaptive modes/targets, buy-guard research, MoE mathematics. | Strategy research owner; comparative evidence beats the stipulated baseline, then 60-90 days of shadow evidence before live use. | Falsifiable evaluation, fresh input coverage and relevant ADR; fixed target modes and current MoE remain shipped. |
| G9 | Phase 9 equities; margin/futures remain separate gates. | Operator + Phase 9 designer; accepted 2.1 close before equities design. | Fresh official API/account/session/settlement/day-trading/tax review; new equity-risk ADR, then separately approved capital/activation. Preserve standing experience gates for margin/futures. |
| G10 | Demand/profile-triggered catalog below. | Named area owner; each row retains its specific source trigger. | Focused design and proportional validation when scheduled; no blanket implementation of the historical catalog. |

## Catalog crosswalk

The tables below exhaust the `###` candidate headings in the nine historical
candidate files. IDs are stable within this index; append new IDs rather than
renumbering. A row marked shipped points to the existing roadmap/ADR evidence and
has no new implementation trigger. A split row explicitly retains its residual.
For a parked row, its linked source's detailed trigger and acceptance constraints
remain attached; the current controlling gate here supersedes obsolete pre-v1.0
dates. A future implementer must record the resulting receipt in the roadmap.

Area owners: engine/adaptive = strategy integrator; observability = operations
integrator; operator-UX = UI/operator integrator; infrastructure = tooling integrator;
news/external = provider integrator; harvester = treasury integrator; trading scope
= operator + strategy designer. G/N ownership and stronger gates take precedence.

### adaptive-grid

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| A01 | [Import the local Kraken historical dump into the DB (one-time bulk seed)](../release/v1.1/adaptive-grid.md) | Shipped importer; G3 retains missing canonical Q2 import/coverage operations. |
| A02 | [Operator-initiated re-anchor command](../release/v1.1/adaptive-grid.md) | Shipped: operator-gated re-anchor. |
| A03 | [Heuristic experts for the other advisor roles (risk / news / arbitrator)](../release/v1.1/adaptive-grid.md) | G10: accepted zero-cloud/offline MoE use case; role-specific behavior and fallback evidence. |
| A04 | [cli/observe --backfill + auto gap-fill — ✅ shipped in v1.1 (2026-05-25)](../release/v1.1/adaptive-grid.md) | Shipped: backfill/gap-fill substrate. |
| A05 | [cli/observe --backfill: v1.1 ergonomics + scenario catalog](../release/v1.1/adaptive-grid.md) | Shipped: seven backfill ergonomics slices. |
| A06 | [Proper OHLC + technical analysis indicators for the advisor](../release/v1.1/adaptive-grid.md) | Shipped: OHLC/TA input spine; current data coverage remains G3. |
| A07 | [Advisor outcome tracking — close the recommendation feedback loop](../release/v1.1/adaptive-grid.md) | Shipped: outcome ledger/evaluator/scoreboard; canonical scoring readiness remains G3. |
| A08 | [Chaos Gremlin — a loose-reasoning advisor, scored not applied](../release/v1.1/adaptive-grid.md) | Shipped: Gremlin observe-only; reseating waits for G3 ledger evidence. |
| A09 | [LLM Historian — long-horizon pattern recognition over weeks/months/years](../release/v1.1/adaptive-grid.md) | G3: quarter coverage, canonical scoring and Historian design. |
| A10 | [Market regime detector — explicit classifier for downstream advisors](../release/v1.1/adaptive-grid.md) | G8: comparative evidence then 60-90 day shadow gate. |
| A11 | [Auditor / strategy + recommendation evaluation tool](../release/v1.1/adaptive-grid.md) | Shipped: config replay and recommendation-scoring implementations; G3 canonical run remains. |
| A12 | [Regime-aware grid modes — adapt grid shape without timing the market](../release/v1.1/adaptive-grid.md) | G8: detector track record, outcomes and own ADR. |
| A13 | [Confidence-driven grid extension — operator-approved buy-the-dip](../release/v1.1/adaptive-grid.md) | G7/G8: calibrated confidence, regime/outcome evidence, hard budget and operator approval. |
| A14 | [Configurable counter-order target (advisor-driven strategy regime)](../release/v1.1/adaptive-grid.md) | Shipped: spacing_up/top_sell modes. G8 retains advisor-driven adaptive target selection. |
| A15 | [`cli/auto-tune` daemon](../release/v1.1/adaptive-grid.md) | G7: demonstrated trust/use case plus ADR removing the operator-trigger requirement. |
| A16 | [Bot learning — discussion stub (full design TBD)](../release/v1.1/adaptive-grid.md) | G7: 60-90 days of usable outcomes and a scoped learning design; RL remains rejected. |
| A17 | [`cli/screener` — symbol-opportunity scanner (operator: "trufflehunt")](../release/v1.1/adaptive-grid.md) | Shipped: screener v1. G10: v1.5 needs spread/volume inputs; v2 needs an accepted RSI/ADX/BB use case. |
| A18 | [MoE aggregation mathematics — question stub (no design)](../release/v1.1/adaptive-grid.md) | G3/G8: real panel corpus for agreement; usable outcome ledger for performance claims. |
| A19 | [A "buy guard" (cost-basis guard's missing other half) — question stub (no design)](../release/v1.1/adaptive-grid.md) | G8: revisit with accepted re-anchor/capital drift work; Auditor evidence before any buy gate. |

### engine

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| E01 | [Bug: ADR-038 fee-drift check false-positives on dust trade fragments](../release/v1.1/engine.md) | G10: dust-fee false-positive review is eligible after an engine touch; require fragment-specific fixtures before changing the fee tripwire. |
| E02 | [Storage caching layer](../release/v1.1/engine.md) | G10: storage dominates >100ms ticks or hot-read p99 exceeds 10ms; profile before caching. |
| E03 | [Async query parallelism (asyncio.gather over symbols)](../release/v1.1/engine.md) | G10: demonstrated multi-symbol tick-budget violation; verify ordering/caps before parallelism. |
| E04 | [Batch APIs (`save_orders_bulk`, `save_trades_bulk`)](../release/v1.1/engine.md) | G10: a real workflow needs more than 100 writes; prove transaction/failure behavior. |
| E05 | [Engine tick latency budget alarming](../release/v1.1/engine.md) | G1/N4: operator-observed dropped ticks and an agreed alert consumer; measured threshold and bounded alerts. |
| E06 | [Server-side dead man's switch (`CancelAllOrdersAfter`) — ✅ SHIPPED 2026-06-01 (v1.1, ADR-021)](../release/v1.1/engine.md) | Shipped: ADR-021 plus later DMS framing/recovery fixes. |
| E07 | [WebSocket real-time updates (private + public channels)](../release/v1.1/engine.md) | G10: storage no longer dominates and REST limits approach under measured load; protocol/reconnect design first. |
| E08 | [System status awareness (`/0/public/SystemStatus`)](../release/v1.1/engine.md) | Shipped: P1 exchange-status awareness; preserve fail-safe behavior. |
| E09 | [Session-loss-cap cool-down period](../release/v1.1/engine.md) | Shipped: ADR-024 cool-down; never automatic restart after a loss cap. |
| E10 | [Capital utilization — added capital and realized profit never reach the trading envelope](../release/v1.1/engine.md) | G6: capital reports exist; automatic envelope/order-floor allocation remains a policy decision. Prior ordermin incident makes review eligible, not automatic tuning. |
| E11 | [Slippage / spread guard before placement](../release/v1.1/engine.md) | Shipped: ADR-025 spread guard. |
| E12 | [SQLite concurrency stress test](../release/v1.1/engine.md) | G10: before Phase 9 write-volume expansion, or observed lock contention; bounded concurrent-reader/writer stress evidence. |
| E13 | [operator.db SQLite write-contention: busy_timeout + retry-on-lock](../release/v1.1/engine.md) | Partly shipped: connection busy timeout. G10 retains broader contention/retry work after stress evidence or recurring lock warnings. |
| E14 | [Order-lifecycle fill-vs-cancel + partial-fill recovery (reconciler + F1) — blueprint](../release/v1.1/engine.md) | Shipped: ADR-023 reconciliation and partial-fill recovery. |
| E15 | [Slippage / spread guard — blueprint](../release/v1.1/engine.md) | Duplicate E11: ADR-025 shipped; preserve the blueprint as historical design. |
| E16 | [Session-loss-cap cool-down — blueprint](../release/v1.1/engine.md) | Duplicate E09: ADR-024 shipped. |
| E17 | [Mid-session reconciliation](../release/v1.1/engine.md) | Partly shipped: daily reconciliation. G10 retains new mid-session repair only on demonstrated drift; no extra daemon by default. |
| E18 | [`cli/reconcile` background worker — ledger-level diff](../release/v1.1/engine.md) | Shipped through cli/maintenance reconcile and the manual trade-history tool; duplicate worker proposal. |
| E19 | [Graceful-shutdown timeout for daemons (`cli/web` et al.) — ✅ shipped in v1.0 (2026-05-23)](../release/v1.1/engine.md) | Shipped: graceful daemon shutdown timeout. |
| E20 | [`cli/preflight` ADR-003 key-scope verification](../release/v1.1/engine.md) | Shipped: trade-key scope preflight and its orchestration regression. |
| E21 | [Partial-grid placement: WARN → INFO with degraded-state context](../release/v1.1/engine.md) | Shipped logging substrate; C1 completes the current starvation explanation. |
| E22 | [Zero-order layout starvation: back-off / re-park instead of per-tick retry — ✅ SHIPPED 2026-08-09 (P3 slice 11)](../release/v1.1/engine.md) | Shipped retry/logging substrate; C1 adds truthful persisted/rendered diagnostics. |
| E23 | [Sell-side-only extension while offside-high — take profit into strength (operator question 2026-09-03; ADR-006 amendment)](../release/v1.1/engine.md) | G5: proposed ADR-042; six-symbol reconciliation and lifecycle/net-margin gates first. |

### external-triggers

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| X01 | [CryptoCompare 90-day evaluation outcome](../release/v1.1/external-triggers.md) | Retired: CryptoCompare was disabled after paid-only change; the old evaluation date is historical. |
| X02 | [Kraken API changes](../release/v1.1/external-triggers.md) | N5/GH97: contract failure or vendor drift; legacy funding deprecation now requires an explicit decision. |
| X03 | [Kraken trading fee changes](../release/v1.1/external-triggers.md) | Standing ADR-038 fee checks: account TradeVolume and per-fill tripwire, plus quarterly backstop. Old quoted rates are historical. |
| X04 | [OpenClaw integration — wobblebot as a callable tool](../release/v1.1/external-triggers.md) | G10: OpenClaw research complete; demonstrated read-only operator workflow and separately authorized integration before code. |

### harvester

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| H01 | [⚠️ Harvester `--execute` replay guard (P1 — highest-blast-radius hole)](../release/v1.1/harvester.md) | Shipped: ADR-026 replay guard; preserve fail-closed post-effect ambiguity. |
| H02 | [⚠️ Harvester-key separateness + withdraw-scope verification (P1)](../release/v1.1/harvester.md) | Shipped: key separation/scope gate; repeat on key rotation/security audit. |
| H03 | [Harvester reconciliation](../release/v1.1/harvester.md) | G10: observed withdrawal-ledger drift; reconciliation design before repairs. |
| H04 | [Harvester gate's eighth defense layer: cumulative daily total](../release/v1.1/harvester.md) | Shipped: cumulative daily withdrawal cap in execute gate. |
| H05 | [Harvester top-up deposits — keep a minimum exchange USD balance for trading](../release/v1.1/harvester.md) | G10: accepted sustained-accumulation feature and a viable deposit-initiation API; new ADR plus ADR-003 ratification before money movement. |

### infrastructure

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| I01 | [Kraken API schema drift coverage](../release/v1.1/infrastructure.md) | N5/C4: public contract checks exist; private/scheduled coverage needs explicit safe test-lane and watcher ownership. |
| I02 | [CI / GitHub Actions](../release/v1.1/infrastructure.md) | Shipped: test-gated CI and image publication. G10 retains wheel distribution only if explicitly requested; PY06 exceptions are N2. |
| I03 | [portainer-mcp: expose AutoUpdate flags on stack creation](../release/v1.1/infrastructure.md) | G10: Portainer integration owner, separate repo authorization when a git-stack AutoUpdate workflow is required. Current WobbleBot stack is file-based. |
| I04 | [Tighten schema-drift coverage for canonical profiles](../release/v1.1/infrastructure.md) | Shipped: canonical-profile and strict schema checks. NAS/operator-file verification remains a deployment gate. |
| I05 | [Multi-arch GHCR image builds](../release/v1.1/infrastructure.md) | G10: a real non-amd64 target; platform build and smoke-test evidence. |
| I06 | [Test count growth](../release/v1.1/infrastructure.md) | Standing maintenance: add a regression for each reproduced defect; no arbitrary test-count target. |
| I07 | [Python 3.14+ compatibility](../release/v1.1/infrastructure.md) | Partly shipped: Docker 3.14 runtime. N2 retains reproducible Python-floor/runtime and hosted-platform gates. |
| I08 | [SQLCipher — database encryption at rest](../release/v1.1/infrastructure.md) | G10: backups/storage cross trust boundaries; hosting decision, key management and verified recovery before SQLCipher. |
| I09 | [Test fixture consolidation — bare ":memory:" SQLite storage](../release/v1.1/infrastructure.md) | G10: a real fixture-wide setup rule beyond connect(); otherwise keep local fixtures. |
| I10 | [WiredSnapshot base class + load_with_degrade helper for web routes](../release/v1.1/infrastructure.md) | G10: new dashboard consumer needs another unwired-fallback copy; reconcile existing field names with tests before extraction. |
| I11 | [GitHub workflow — flag new LLM model releases](../release/v1.1/infrastructure.md) | N5/GH22: per-seat provider coverage and change detection; separate from model-selection campaigns. |
| I12 | [Anthropic prompt-cache `cache_control` (deferred per ADR-033)](../release/v1.1/infrastructure.md) | G10: deployed Anthropic cadence fits cache TTL or Phase 9 increases volume; cache-aware accounting already shipped, validate pricing and inputs before prompt changes. |
| I13 | [One-off operator scripts accumulate in the NAS `data/` dir](../release/v1.1/infrastructure.md) | G10: next authorized NAS housekeeping or before manual reconciliation; use shipped tools path, inventory before deleting old scripts. |
| I14 | [Audit: hardcoded facts that should be data / DB-driven](../release/v1.1/infrastructure.md) | Shipped audit; residual FH1-FH3 and G6 retain the four-homes decisions. |

### news-pipeline

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| NW01 | [Auto-pause on news-role HIGH risk](../release/v1.1/news-pipeline.md) | G7: calibrated outcomes and explicit ADR-002 exception before auto-pause. |
| NW02 | [Kraken status news adapter — first-party exchange-impact feed](../release/v1.1/news-pipeline.md) | Shipped: first-party Kraken status news adapter. |
| NW03 | [News pipeline gap audit vs Kraken Pro's 16 sources](../release/v1.1/news-pipeline.md) | Partly shipped: attribution substrate. G10: degraded news signal or requested source-quality metrics; preserve retired CryptoCompare posture. |

### observability

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| O01 | [Stale-heartbeat Discord push alert](../release/v1.1/observability.md) | Shipped: heartbeat alerts; independent observer remains N4. |
| O02 | [Anomaly detector daemon — cross-DB outlier watcher](../release/v1.1/observability.md) | G1: actual per-signal baseline and detector design; current backup gaps do not clear it. |
| O03 | [Backup verification — restoration smoke test](../release/v1.1/observability.md) | Shipped: maintenance backup restoration verification. |
| O04 | [Data retention policy](../release/v1.1/observability.md) | Shipped: ADR-036 archive/retention; keep forensic data protected. |
| O05 | [Prune cycle logs a WARNING on every same-day restart](../release/v1.1/observability.md) | G10: next maintenance touch, verify whether same-day prune warning still recurs before changing it. |
| O06 | [Daily summary email or Discord-DM](../release/v1.1/observability.md) | G10: operator misses days and needs a digest; define channel, contents and cadence. |
| O07 | [Per-cycle LLM call tracing](../release/v1.1/observability.md) | Shipped: P4.4a trace IDs and cost grouping. |
| O08 | [Connectivity retry policy audit](../release/v1.1/observability.md) | Shipped audit/hardening substrate; C2 completes the named exceptions, N5 retains Ollama/provider correctness. |
| O09 | [Logging-quality audit + enrichment pass (application-wide) — historical installment-1 heading](../release/v1.1/observability.md) | Shipped: all three installments, completed module sweep and central redaction; see roadmap Slice 17. G10 retains only decimal-format candidates at the next focused logging review. |
| O10 | [Disk space awareness in the anomaly detector](../release/v1.1/observability.md) | G1: bundle disk signal with an accepted anomaly/operations consumer. |
| O11 | [Ollama hang detection audit](../release/v1.1/observability.md) | Shipped: P3 timeout/health audit. N5 retains the newer adapter-contract findings. |
| O12 | [Remote backup destinations (S3 / rclone / SFTP)](../release/v1.1/observability.md) | G10: operator requires independent NAS durability; choose destination, encryption, credentials and restore test. |
| O13 | [Prometheus / metrics export](../release/v1.1/observability.md) | G10: operator adopts Grafana; define bounded metrics and overhead before adding exporter. |
| O14 | [PagerDuty / email / SMS alerting fallback](../release/v1.1/observability.md) | N3/N4: independent delivery design; operator demand following missed Discord alert controls extra channels. |
| O15 | [Solo-operator incident runbook](../release/v1.1/observability.md) | Shipped: incident runbook; update when a new incident changes recovery practice. |
| O16 | [Cost-honesty dashboard — bot's ROI against its own infrastructure](../release/v1.1/observability.md) | Shipped: P4.7 cost-honesty dashboard. |
| O17 | [LLM health check on the /health page — ✅ SHIPPED 2026-08-09 (P3 slice 10)](../release/v1.1/observability.md) | Shipped: provider health surface; residual credential ownership is N1. |

### operator-ux

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| U01 | [Web UI first-run admin user wizard](../release/v1.1/operator-ux.md) | G10: a scheduled friend onboarding; Tier 0 evidence precedes an admin wizard. |
| U02 | [Multi-coin defaults in `cli/recalibrate`](../release/v1.1/operator-ux.md) | G10: operator rejects equal weighting in multi-coin recalibration; explicit allocation design and dry-run evidence. |
| U03 | [Discord command shortcuts — deterministic fast path in front of the LLM parse — ✅ SHIPPED 2026-09-03](../release/v1.1/operator-ux.md) | Shipped: deterministic shortcuts and parser attribution. |
| U04 | [Discord confirmation UX: replace emoji reactions with UI buttons](../release/v1.1/operator-ux.md) | Shipped: persistent confirmation buttons; C2.4 repairs refusal transport. |
| U05 | [Web UI per-entity action buttons (generic "decide + audit" pattern)](../release/v1.1/operator-ux.md) | Partly shipped: trading/harvest actions. G2 retains advisor Apply/Approve-Reject ownership. |
| U06 | [Web UI read-views the operator wishes existed](../release/v1.1/operator-ux.md) | G10: a named unanswered operator question; add only the necessary read view. |
| U07 | [Status card per-order delta column](../release/v1.1/operator-ux.md) | Shipped: status per-order distance/delta view. |
| U08 | [Footer: "update available" indicator](../release/v1.1/operator-ux.md) | Shipped: footer release-check indicator. |
| U09 | [State-aware per-symbol pause/resume buttons — ✅ SHIPPED 2026-08-09 (P3 slice 6)](../release/v1.1/operator-ux.md) | Shipped: state-aware pause/resume. |
| U10 | [Notifications surface — server-side read state + deep linking](../release/v1.1/operator-ux.md) | Shipped: server-side read state and deep links. |
| U11 | [Re-anchor banner — action button + snooze — ✅ SHIPPED 2026-08-09 (P3 slice 5)](../release/v1.1/operator-ux.md) | Shipped: re-anchor action and snooze. |
| U12 | [Re-anchor reachable without the banner — per-symbol anchor button — ✅ SHIPPED 2026-09-03](../release/v1.1/operator-ux.md) | Shipped: per-symbol anchor button. |
| U13 | [Offside badge tooltip — say WHY the symbol is offside — ✅ SHIPPED 2026-09-03](../release/v1.1/operator-ux.md) | Shipped: offside explanation and truthful start timestamp. |
| U14 | [Hide a symbol card — eye-icon visibility toggle — ✅ SHIPPED 2026-09-04](../release/v1.1/operator-ux.md) | Shipped: hidden-symbol table and visibility toggle. |
| U15 | [Hide a symbol card — eye-icon visibility toggle (operator note 2026-09-03, for later review)](../release/v1.1/operator-ux.md) | Duplicate U14: historical sketch superseded by shipped design. |
| U16 | [Follow-ups from the 2026-09-03 adversarial review (filed, not built)](../release/v1.1/operator-ux.md) | Shipped post-2.0.4 Groups 0-3; rendering evidence is synthetic/operator-reported as the original receipt states. |
| U17 | [Pause stays non-destructive (+ candidate "halt" compound)](../release/v1.1/operator-ux.md) | Partly shipped: non-destructive pause warning. G10 retains a confirmed halt compound only if manual composition proves cumbersome. |
| U18 | [Re-anchor viability weighting (probability-of-success on the banner)](../release/v1.1/operator-ux.md) | Partly shipped: v0 activity statistic. G10 retains weighted viability after an accepted model/evidence design. |
| U19 | [Status card recent-fills section enhancement](../release/v1.1/operator-ux.md) | Shipped: recent-fill freshness/metadata. |
| U20 | ["Today's PnL" — split cycle realization-day from earning-day](../release/v1.1/operator-ux.md) | Shipped: P3.20 realization/earning-day presentation. |
| U21 | [Status card multi-coin layout](../release/v1.1/operator-ux.md) | Partly shipped: multi-coin cards. G10 retains width/wrap redesign at roughly eight or more coins; C1 covers configured empty cards. |
| U22 | [Symbol-card price + indicator for parked / no-order symbols](../release/v1.1/operator-ux.md) | Shipped substrate plus C1 configured/no-history symbol cards; no unpriced symbol is presented as current. |
| U23 | [Whole-UI design review — punch list (2026-06-03)](../release/v1.1/operator-ux.md) | Partly shipped: June design baseline and P3 UI work. G10 retains explicitly open typography/layout/chart refinements at the next accepted UI review. |
| U24 | [Discord response quality: data + presentation + model attribution — ✅ shipped in v1.0 (2026-05-24)](../release/v1.1/operator-ux.md) | Shipped: Discord response presentation. |
| U25 | [Discord status_report tally section: compact table instead of stacked fields — ✅ SHIPPED 2026-08-10 (P3 slice 18)](../release/v1.1/operator-ux.md) | Shipped: compact status tally. |
| U26 | [Bespoke notification-card renderers (proactive push embeds) — ✅ SHIPPED 2026-08-09 (P3 slice 7)](../release/v1.1/operator-ux.md) | Shipped: typed notification cards. |
| U27 | [Command-result echo to Discord (a new `notify()` raise site) — ✅ SHIPPED 2026-08-09 (P3 slice 7)](../release/v1.1/operator-ux.md) | Shipped: command-result echo and command catalog; future catalog additions require paired coverage. |
| U28 | [`weather_report` query — market-trend summary across multiple days](../release/v1.1/operator-ux.md) | Shipped: P4.5 weather report. |
| U29 | [`AssistantPort.summarize` — cloud provider implementations](../release/v1.1/operator-ux.md) | G2: actual cloud summary consumer plus cost gate. |
| U30 | [Mode-parameterized webui — reuse the dashboard for live + shadow](../release/v1.1/operator-ux.md) | Partly shipped: mode badge/source. G10: operator wants to watch a shadow run through the existing UI; resolve mode/data-source/firewall plumbing first. |
| U31 | [One-command daemon orchestrator (`cli/up` wrapper)](../release/v1.1/operator-ux.md) | Superseded for NAS lifecycle by Compose. G10 retains a local wrapper only for a demonstrated repeated-start workflow; no new dependency preapproved. |
| U32 | [Always-on hosting topology — decouple from operator laptop](../release/v1.1/operator-ux.md) | Shipped NAS hosting. G10: alternative topology requires an operator decision, including where Ollama lives. |
| U33 | [Friend-deployment onboarding — guided setup (dummy-proof scope)](../release/v1.1/operator-ux.md) | G10: friend schedules setup; learn from Tier 0 before building later onboarding tiers. |
| U34 | [Session cookie keyed by user.id, not username](../release/v1.1/operator-ux.md) | G10: announced migration or multi-user need; session-identity ADR and explicit session invalidation plan. |
| U35 | [Multi-factor authentication (TOTP) for the web UI](../release/v1.1/operator-ux.md) | G10: shared/multi-user/internet-exposed deployment; threat model and recovery design before MFA. |
| U36 | [Content-Security-Policy header](../release/v1.1/operator-ux.md) | Shipped: Content-Security-Policy middleware. |
| U37 | [Multi-operator web auth](../release/v1.1/operator-ux.md) | G10: another operator needs production access; roles, identity and authorization design. |
| U38 | [Richer charts in web UI (price history, PnL curves)](../release/v1.1/operator-ux.md) | G10: operator cannot answer a trend question from current tables; specific chart and data semantics. |
| U39 | [Math-specialist LLM integration paths](../release/v1.1/operator-ux.md) | G4/G8: a concrete specialist role is prioritized; repair probes and evaluate that role, not general assistant fit. |
| U40 | [Reasoning-model support — DROPPED 2026-05-26 after v2 follow-up](../release/v1.1/operator-ux.md) | Declined: preserve the two failed redesign receipts; reopen only on materially different evidence and explicit scope. |
| U41 | [Foreign-language operator support -- audit + test coverage](../release/v1.1/operator-ux.md) | G10: non-English operator demand or observed non-Latin corruption; bounded locale/encoding audit. |
| U42 | [Help directory / FAQ / dictionary (web + Discord + CLI)](../release/v1.1/operator-ux.md) | G10: operator selects the proposed help/FAQ scope; terminology and cross-surface navigation acceptance first. |

### trading-scope

| ID | Source candidate | Disposition / next trigger and evidence |
| --- | --- | --- |
| T01 | [High-frequency grid on high-volatility pairs](../release/v1.1/trading-scope.md) | G10: explicit instrument expansion and adequate tax/accounting capacity; venue/liquidity/fee validation. |
| T02 | [Multi-asset / multi-exchange expansion](../release/v1.1/trading-scope.md) | Partly shipped: multi-coin Kraken spot. G10: separately approved assets/exchange allocation and adapter design. |
| T03 | [Configurable quote currency (non-USD: EUR / GBP / ...)](../release/v1.1/trading-scope.md) | G10: real non-USD deployment demand; audit quote assumptions and all money/display fields, not just a selector. |
| T04 | [Kraken Securities equities support (Phase 9 committed track)](../release/v1.1/trading-scope.md) | G9: accepted 2.1 close then fresh equity-risk design; capital/activation separate. |
| T05 | [Margin trading support](../release/v1.1/trading-scope.md) | G9: all four margin experience/education/paper/approval gates in standing-rules; no phase-close shortcut. |
| T06 | [Futures trading support (long-short grid variant)](../release/v1.1/trading-scope.md) | G9: margin gates plus all three futures-specific gates in standing-rules; no phase-close shortcut. |

## Remaining source registers

These rows reconcile the additional bounded sources without treating structural
headings, implementation examples or historical tables as new feature requests.

| ID / source | Disposition | Trigger, owner and acceptance |
| --- | --- | --- |
| FH1-FH3 — [four-homes audit](../release/v1.1/four-homes-audit.md) | Retain model compatibility, reasoning-pattern and news-symbol externalization candidates; keep safety pricing/fees in code. Duplicate hardcoded-facts entry is I14. | Tooling/provider owner: friend deployment or concrete config drift, one slice at a time. Verify fail-soft/empty overrides and news-symbol consistency; G6 governs writable POLICY. Source secondary findings retain their existing drift/duplication triggers. |
| R1 — `_common` argparse extraction | G10 retained. | CLI owner: next relevant touch; move the cohesive parser concern only with argument behavior preserved. |
| R2 — config CLI module size | Resolved: the local pylint disable and cohesive-module rationale already exist in `src/wobblebot/config/cli.py`. | Config owner reconsiders only if a new section changes that cohesion; no outstanding split-or-disable task. |
| R3 — `grid_ceiling` helper | G10 retained. | Strategy owner: third consumer; pin ascending-level invariant. |
| R4 — OHLC limit semantics | G10 retained. | Storage owner: before first production caller uses `limit`; decide newest-N vs current oldest-first contract. |
| R5 — interval error formatting | G10 retained. | Data owner: next relevant touch; one vocabulary over `ALLOWED_INTERVALS`. |
| R6 — Auditor private mock override | G10 retained. | Replay owner: next relevant touch; explicit fill-on-place seam with equivalent replay behavior. |
| R7 — ATR pass-through | G10 retained. | TA owner: next relevant touch; preserve numerical output. |
| R8 — [P1 narrow test gaps](../release/v1.1/README.md#p1-test-hardening--consequenceorchestration-coverage-test-honesty-audit-2026-06-02) | Retain intra-tick DMS order, symbol-scoped exposure, compound Harvester failure/current-balance-none and cross-symbol parked isolation as distinct candidates. | Safety owner: relevant behavior changes or incident evidence; prove consequences with isolated mutations. Existing closed P1 hardening rows stay closed. |
| P0/P1/P2 and P3/P4 historical tables — [v1.1 index](../release/v1.1/README.md) | Completed substrate remains shipped; P3 anomaly/disk, advisor action residuals and P4.6 remain G1-G3. Detailed blueprints retain provenance. | Roadmap receipts control completion, not old future-tense prose. Current repair queue is C2; do not re-run the old v1.0 tag gate. |
| REL1 — [2.0 release plan](release-2.0-plan.md) | Historical ceremony and shipped isolation/redaction superseded; residual slices map to N1-N5. | Release integrator: C5 uses the current candidate and file-stack state, not the old plan's tag/ADR placeholders. |
| REL2 — [post-2.0.4 plan](post-2.0.4-backlog-plan.md) | Groups 0-3 shipped/settled; Group 4 maps to G5. | Preserve the original synthetic-render/operator-report limitations; no replay of shipped groups. |
| PB1-PB13 — [probe punch list](../reference/probe-battery-punch-list.md) | All 13 carried forward individually under G4; no silent closure by aggregate model score. | Model-review owner: before another paid seat campaign; verify each numbered finding's own regression/measurement. |
| IDEA1/IDEA2 — [future ideas](future-ideas.md) | MoE and Discord proposals superseded by shipped architecture. | New MoE math is A18; new Discord lifecycle is N3. No duplicate feature tickets. |
| RULE1 — [standing rules](../release/v1.1/standing-rules.md) | Preserve SDK decline; stop-loss/take-profit/staking/conditional-order declines; margin/futures experience gates. | Operator + architecture owner: only the documented trigger plus explicit decision can reopen them. No runtime dependency adoption follows from this index. |

The pre-2.0.4 pause control on untraded symbols remains a documented harmless
no-op UX limitation (U17/UI owner, next accepted control-surface review). The
readiness and model-watch follow-ups outside this repository require their own
authorization; they are not delegated or scheduled by this document.
