# Full-product completion baseline

Work item: `CarlDog/wobblebot:product-completion`. Baseline source commit:
`9a42a790f670eb74df0cc381c6a484c7324e6b90`. Initial branch `work`, clean tracked
and untracked state; implementation branch `codex/product-completion`. The cloud
checkout excludes unpushed laptop changes. Current receipts, command results and
blocker dispositions belong in the [roadmap](roadmap.md#cloud-product-completion-verification).
This file defines the finite acceptance inventory, not a second release ledger.

## Authority and definition of done

The [requirements](requirements.md) own FR/NFR meaning; accepted
[ADRs](../architecture/decisions.md) and
[operational decisions](../architecture/ratified-decisions.md) resolve architecture
and supersession. The roadmap owns phase order and receipts. The accepted
[closeout sequence](2.0-closeout-and-2.1-entry-plan.md), [backlog](backlog.md),
and explicit adoption decisions distinguish committed work from candidates.
Neither an open issue nor code presence independently establishes adoption.
The old five-milestone shape is reconciled to the later eight-phase roadmap;
M5's dashboard/recovery/release criteria remain attached to Phases 7/8.

Done means every adopted behavior below is implemented and integrated, all
required development gates pass (or the operator explicitly accepts an exception),
documentation is accurate, and changes are preserved/accounted for. Historical
production receipts are provenance, not fresh cloud-environment results.
A blocked or proposed row is never counted as complete. Later phases remain
in the inventory even when their entry/design/data gates prevent implementation.

## Canonical requirements

D = documented acceptance (summarized here, full source remains binding).
I = inferred verification, additional to rather than replacing D. Existing
implementation paths are relative to `src/wobblebot`; test paths to `tests`.
The verification/blocker column names the evidence lane in the roadmap receipt.

| ID / source | D: acceptance | Existing implementation | I: verification / remaining work and blockers |
| --- | --- | --- | --- |
| FR-001 / requirements | Per-asset configurable micro-grid, limit orders through adapter | `services/grid_engine.py`, grid helpers, `adapters/kraken_exchange.py` | Unit grid math/counter/fee tests; offline 1,000-tick SQLite integration; real API B2 |
| FR-002 / requirements | Concurrent whitelisted assets | `cli/live.py`, symbol-scoped engine state | Multi-symbol cap/isolation tests; synthetic integration; production account B2 |
| FR-003 / requirements | Per-asset, total exposure and daily spend limits | `services/grid_engine.py`, `config/safety.py` | Refusal/valuation/cost-basis/inventory tests; no weakening to place orders |
| FR-004 / requirements | Summarize metrics, validate/store advisor JSON | `services/summary_builder.py`, advisor adapters, `cli/advise.py` | Parser/provider failure, summary and persistence tests; paid/provider qualification B2 |
| FR-005 / requirements; ADR-012 | Opt-in whitelist/bounds/global safety; before/after suggestion audit | `cli/apply.py`, `services/auto_apply.py`, settings rewriter | Disabled/default, blocked news/gremlin, invalid values and comment-preserving writes; hot-tune daemon is G7 |
| FR-006 / requirements; ADR-004 | Threshold balance monitoring and reasoned directional proposals | `services/harvester.py`, `cli/harvest.py` | Surplus/deficit/cap tests. Bank deposits operator-pushed; no banking adapter |
| FR-007 / requirements; ADR-026/034 | Opt-in guarded withdrawals, per-action/day/liquidity limits | `cli/harvest_execute.py`, durable claims and web-approved queue | Replay/concurrency/ambiguous result/firewall tests; live withdrawal B2, no diagnostic transfer authorized |
| FR-008 / requirements | Structured events, persisted histories, dashboard metrics | logging, SQLite, web routes, maintenance | Logging contract/redaction, rendered dashboards and database tests; production delivery B2 |
| NFR-001 / requirements | Deterministic core for same input | pure grid/metrics, historical auditor | Fixed-fixture replay and offline integration |
| NFR-002 / requirements; ADR-001/002/003/041 | Advisor advisory only; Harvester sole withdrawal authority; ports isolate modules | ports, AST boundary guard, Compose grants, approval union | Architecture/capability/firewall tests; residual mount isolation N1 retained |
| NFR-003 / requirements | Reasonable NAS CPU/memory | schedules, WAL/indexes, storage profiler | Local profiling is directional; target NAS resource acceptance B2 |
| NFR-004 / requirements; ADR-018/023/046 | Restart safely, reload orders, reconcile without duplicate effects | reconciler, terminal-order resolver, pending-fill markers | Restart/partial-fill/missing-trade/upgrade tests; broader lifecycle N3 retained |
| NFR-005 / requirements; ADR-009 | Config/env-controlled behavior with documented defaults/ranges | Pydantic config, examples, profiles, CLI overrides | Config suite and deprived-env matrix; private operator drift B2; Linux Makefile repair D1 |

## Adopted phase inventory

Each stage below incorporates its complete corresponding roadmap paragraph and
linked stage design acceptance criteria. The short label is an index, not a
replacement specification. Phases 1-8 have dated historical receipts in the
roadmap and phase summaries. Current local evidence is the full suite plus the
lanes below; no historical live test is represented as rerun. The actual source
and tests remain the implementation evidence, not the checkmarks alone.

| Phase | Adopted stages (exact roadmap IDs) | Existing surfaces / inferred verification | Remaining verification |
| --- | --- | --- | --- |
| 1 / [roadmap](roadmap.md), phase summary/designs | 1.1 Repo & Scaffolding; 1.2 Hex Core Skeleton; 1.3 Storage & Logging Backbone; 1.4 Kraken Mock & Simulation Mode; 1.5 Phase 1 Integration Check | domain/ports, mock, SQLite, sandbox; real CLI sandbox | Fresh external/production checks B2; container gate B3 where applicable |
| 2 / [roadmap](roadmap.md), phase summary/designs | 2.1 Kraken Adapter (Read‑Only + Minimal Data Collector); 2.2 Micro-Grid Engine; 2.3 Live Paper Mode / Tiny‑Size Mode; 2.4 Multi‑Asset Support; 2.5 Phase 2 Integration Check | Kraken/grid/caps/live; offline grid integration and adapter tests | Fresh external/production checks B2; container gate B3 where applicable |
| 3 / [roadmap](roadmap.md), phase summary/designs | 3.0 Observer & Shadow Mode; 3.1 Data Collector & Metrics (v2); 3.2 Advisor Port & Single-Model Integration; 3.2.5 News Ingestion; 3.3 Passive Advisory Workflow; 3.4a Mixture of Experts (MoE); 3.4b Optional Auto-Tuning (Guarded); 3.5 Phase 3 Integration Check; 3.6 Operational polish (pre-Phase 4) | observe/shadow/news/metrics/advisor/MoE/apply; services/adapters/CLI suites | Fresh external/production checks B2; container gate B3 where applicable |
| 4 / [roadmap](roadmap.md), phase summary/designs | 4.1 Harvester Domain & Ports; 4.2 Read‑Only Balance Monitoring; 4.3 Passive Mode Transfers; 4.4 Active Mode (Guarded Withdrawals); 4.5 Phase 4 Integration Check | treasury proposals/execution; Harvester guard and persistence tests | Fresh external/production checks B2; container gate B3 where applicable |
| 5 / [roadmap](roadmap.md), phase summary/designs | 5.1 Operator Domain & Ports; 5.2 Discord Transport Adapter; 5.3 Operator Assistant (Ollama); 5.4 Engine Integration; 5.5 Outbound Notifications; 5.6 `cli/operator` Daemon; 5.7 Phase 5 Integration Check | Discord intent/approval/query; offline operator integration | Fresh external/production checks B2; container gate B3 where applicable |
| 6 / [roadmap](roadmap.md), phase summary/designs | 6.1 Shared cloud-LLM infrastructure; 6.2 Anthropic adapter; 6.3 OpenAI adapter; 6.4 Google adapter; 6.5 Phase 6 Integration Check | cloud adapters/cost/retry/failover; synthetic provider/ledger tests | Fresh external/production checks B2; container gate B3 where applicable |
| 7 / [roadmap](roadmap.md), phase summary/designs | 7.1 Web app skeleton + auth.; 7.2 Cost + status dashboards + mutation buttons.; 7.3 Advisor + harvester views.; 7.4 News + audit log views.; 7.5 Phase 7 close + integration check.; 7.6 Operational ergonomics: cli/recalibrate. | web auth/CSRF/status/commands/cost/recalibration; rendered HTTP tests | Fresh external/production checks B2; container gate B3 where applicable |
| 8 / [roadmap](roadmap.md), phase summary/designs | 8.0 Deferred Phase 5 audit refactors; 8.1 Reliability & Recovery; 8.2 Background Maintenance Worker; 8.3 Performance & Resource Tuning; 8.4 Phase 8 / v1.0 Release Check; 8.5 Advisor Engine: Heuristic + LLM Cascade; 8.6 Advisor HARDENING + Grid Widen | reconciliation/maintenance/WAL/backup/cascade; failure, migration and recovery tests | Fresh external/production checks B2; container gate B3 where applicable |

## Adopted post-v1 substrate and remaining phases

| ID / source | D: acceptance / implementation boundary | I: checks, remaining work and blockers |
| --- | --- | --- |
| P0 / v1.1 index | Shared logging/grid primitives, simulator/auditor parity, four-home reconciliation | Existing core tests; retain individually gated source findings via catalog below |
| P1 / v1.1 index | DMS, terminal/partial fills, loss-cap cooldown, spread/cost guards, replay protection and ready-now hardening | Existing engine/Harvester regression tests; R8 candidates are not silently promoted to adopted features |
| P2 / v1.1 index | Backfill, bulk history import, OHLC/TA, real-engine Auditor, screener in documented order | Existing importer/replay/TA/screener tests; actual canonical history/scoring G3 |
| P3 / v1.1 index | Shipped operator controls, status/health/UI, confirmations and operational visibility | Existing web/operator/health tests; anomaly/disk G1 and advisor action G2 remain unresolved |
| P4.1-P4.3 / outcome-ledger design, v1.1 index | Persist outcomes, replay evaluator and scoreboard with provenance and fidelity labels | Existing evaluator/outcome/scoreboard tests; actual canonical corpus and scoring G3 |
| P4.4 / p4-completion-plan | Trace clock, directional grading, observe-only Gremlin | Existing trace/context/directional/Gremlin firewall tests |
| P4.5 / p4-completion-plan | Weather trends/report, deterministic fallback, named facts | Existing reports/TA/operator tests; live narrative B2 |
| P4.6 / p4-completion-plan | Read-only long-horizon Historian, findings store and UI after scored corpus/design | Not implemented; G3 requires unavailable corpus and NAS scoring, model/cadence/privacy design |
| P4.7 / p4-completion-plan | Cost honesty, infra declared-vs-unknown, trace grouping, no double-count fees | Existing cost page/evaluator tests |
| C0-C4 / closeout plan | Preserved baseline, truthful starvation diagnostics, four repairs, migration/old-writer safety, full audit and review | Existing implementation and historical review; fresh suite/upgrade verification; D1 setup correction; B2/B4 audit limits |
| C5 / closeout plan | Exact release/deploy/observation identity, individually accepted limitations and formal operator closure | Historical releases exist; formal close pending B1, fresh production evidence B2. Publication not authorized |
| N0 / closeout plan | Accepted 2.0 close, reconciled N1-N5 scope, unique ADR references, first slice criteria | B1 unresolved; do not infer phase acceptance from general historical deployments |
| N1 / closeout plan §5; ADR-041 | Per-service data/config/secret capabilities, WAL/backups/settings consumers; remove unnecessary web cloud keys using authoritative fresh health | Not implemented as full residual scope; B1, then capability design and isolated Compose positive/negative tests (B3) |
| N2 / closeout plan §5 | Hashed pip resolution, runtime/base digest identity, sanitized boot provenance, platform/Python matrix, tooling decision | Not implemented; N1/B1; Linux floor checks do not satisfy hosted Windows or 3.14 matrix |
| N3a / closeout plan §5 | Immutable approval, atomic claims, expiry/mutation rejection, post-effect ambiguity reconciliation | Withdrawal-specific protection exists, broader lifecycle absent; N1-N2/B1. Concurrent/crash-boundary tests required |
| N3b / closeout plan §5 | Durable notification outbox, independent delivery, bounded retries/terminal failure; no exactly-once claim | Not implemented; N3a and G1 consumer coordination; crash-before/after-send tests required |
| N4 / closeout plan §5 | Independent dead/wedged detection, read-only human/JSON doctor, unknown/stale evidence; money daemons page-only | Not implemented; N1-N3/B1. Restart actor authority needs explicit design decision |
| N5a / closeout plan §5 | Correct Ollama envelopes/prompts/errors/truncation/local-only and telemetry | Some maintenance/fallback tests already shipped; reconcile residual against source after B1/N4; no paid probes |
| N5b / closeout plan §5; GH22/GH97 | Deduplicated contract/pricing/model-watch ownership; funding retention/migration decision | Not implemented as complete watcher; N5a/B1, external ownership/access B2; no unapproved schedule |
| 9.0 / roadmap Phase 9, G9 | Fresh official equity/account/session/settlement/day-trading/tax review, new equity-risk ADR | Committed track; unstarted after accepted 2.1 close. May scoping figures are unratified, not requirements |
| 9.1 / roadmap Phase 9 | Asset-class and stock symbol/precision/session metadata and error mappings | Unstarted; 9.0 and confirmed API/account access B2 |
| 9.2 / roadmap Phase 9 | Settlement/day-trading-aware safety with persisted history and startup guards | Unstarted; 9.0 risk/account decisions must precede financial policy implementation |
| 9.3 / roadmap Phase 9 | Earnings-calendar pause windows, override and notification | Unstarted; verified source selection and 9.0 |
| 9.4 / roadmap Phase 9 | Small real equity cycle, full adapter/safety/settlement path | Unstarted; separate capital/activation/trade authorization required |
| 9.5 / roadmap Phase 9 | Tax export and wash-sale accounting with cost UI | Unstarted; verified legal/account/source assumptions and 9.0; no fabricated compliance |
| 9.6 / roadmap Phase 9 | Multi-symbol integration, real earnings event and verified tax export, closing summary | Unstarted; all prior slices and separate live acceptance |

## Complete retained-candidate crosswalk

Every individual row in [backlog.md](backlog.md#catalog-crosswalk), not just its
heading, is incorporated into this inventory with its stable ID, linked original
source, disposition, trigger and acceptance. Its A/E/X/H/I/N/O/U/T registers and
remaining F/R/PB/REL/IDEA/RULE registers must be reconciled before final completion.
The following gates preserve adopted residuals without treating every idea as adopted:

| Gate | Required unresolved evidence / implementation | Current blocker |
| --- | --- | --- |
| G1 | Per-signal baseline coverage/gaps/retention; anomaly and disk consumer/design | Private usable history and consumer decision B2; calendar age insufficient |
| G2 | Advisor actions with settings-writer ownership; cloud summary consumer | ADR-034 ownership and concrete consumer still unratified |
| G3 | Q2 imports, canonical NAS outcome scoring, fidelity/bias tallies, Historian design | No corpus or NAS database access B2; local synthetic scoring is not equivalent |
| G4 | Each PB1-PB13 prerequisite before a paid seat campaign | No new campaign authorized; availability/judgment, caps/order, truncation, temperature, numerics, contested fixtures, provenance remain distinct |
| G5 | Proposed ADR-042 sell extension | Reconciled trade-and-ledger history, retirement policy, net-margin decision and ADR ratification; no speculative sell behavior |
| G6 | Accepted ADR-040 writable POLICY/capital work | Second qualifying edit evidence, refreshed fixture and ownership/failure design unavailable; ADR-044 remains proposed |
| G7 | Auto-tune/news-pause/confidence/learning candidates | Item-specific adoption/ADRs and credible outcomes; not automatically adopted by catalog membership |
| G8 | Regime/Oracle/adaptive/buy-guard/MoE research | Comparative evidence then 60-90-day shadow gates; no result invented |
| G9 | Committed equities track | 2.1 close and 9.0 decisions; retained above, not excluded |
| G10 | Individual demand/profile/consumer-triggered candidates | Each original trigger retained in backlog; no blanket cleanup or feature adoption |

Exclusions: declined SDK/banking abstraction, stop-loss/take-profit/staking/
conditional-order proposals and reinforcement learning remain declined under
standing rules/ADRs. MoE/Discord original ideas are superseded by shipped designs.
Margin/futures/options, long-hold strategies, multi-exchange and separate external
repository/plugin projects lack adoption for this implementation. Optional cache,
refactor, hosting and friend-onboarding entries require their documented triggers.
No exclusion is based merely on cost, difficulty or a later-phase label.

## Blocker keys and continuation contract

- B1: formal phase-entry acceptance is explicitly outstanding in the latest roadmap.
  The initial question combined production-risk acceptance with permission for local
  phase entry. Those decisions must be separated: local N1 work need not imply
  production acceptance. The parent was asked to resolve this against the user's
  express override of routine development checkpoints. No reply is not acceptance. The prior E01-E03 exceptions
  remain accepted only for their original stabilization scope.
- B2: fresh external evidence/access: GitHub API and public Kraken connectivity,
  private configs/keys, canonical OHLC/outcome data, NAS and production observations.
  Do not copy historical counts as current facts or run paid/money operations.
- B3: actual container build/runtime validation. Preserve failed command evidence;
  source-level Compose tests do not substitute for an executed container rehearsal.
- B4: Fleet Kit phase-end skill/standards tooling unavailable in this environment.
  `.agents/skills` is absent; explicit repository audit checks remain executable.
- D1: demonstrated local setup defect: Linux Make targets select a Windows-only
  interpreter. Repair must preserve Windows/override selection and make verification
  non-mutating; synchronize the contributor workflow with current process guidance.

Resume from this branch, inspect `git status`, read the latest roadmap receipt,
and address the named blockers before phase-dependent work. Keep existing code,
financial invariants and all unresolved criteria intact. Never mark a handoff as
product completion.

N1 preparation: [source-derived capability inventory](n1-capability-inventory.md).
