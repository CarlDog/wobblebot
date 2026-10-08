# Agentic broker platforms: later-development research

Researched **2026-10-03 UTC** (2026-10-02 America/Chicago). This brief records
source-backed research supplied by the parent task. It is a future-work reference,
**not adopted implementation scope, an integration approval or a live-test receipt**.
No broker account, credential, dependency, paid probe, support monitor, external
contact or financial action is introduced. Main-branch source observations below
are not asserted to match a released binary without a pinned-source comparison.

## Robinhood: three distinct surfaces

| Surface / source date | Official evidence | What it does not establish |
| --- | --- | --- |
| External Agentic Trading, launched May 27, 2026 | [Launch announcement](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/) describes the hosted MCP endpoint `https://agent.robinhood.com/mcp/trading`, a dedicated funded Agentic execution account, notifications, activity and disconnect controls. [Onboarding](https://robinhood.com/us/en/support/articles/agentic-trading-overview/) documents StreamableHTTP and interactive authentication. | Execution-account isolation is not read-data minimization: documented read access spans all Robinhood accounts, identifiers, holdings and history. Do not connect merely to explore tools. |
| Native Agents and Agent Apps, announced September 29, 2026 | [HOOD Summit announcement](https://robinhood.com/us/en/newsroom/hood-summit-2026/) introduces native Agents and Agent Apps. [Agent Apps](https://robinhood.com/us/en/support/articles/agent-apps/) describes mobile-only datasets, skills and tools. | Recurring strategy execution through Loops is **coming soon**, not verified shipped. External MCP clients do not automatically inherit native premium tools or transferable subscriptions. |
| Cortex engineering, August 25, 2026 | [Architecture account](https://robinhood.com/us/en/careers/blog/loop-stays-dumb-so-the-model-can-be-smart/) describes a single rich-tool loop replacing routed specialists, append-only replay and prompt caching, a core prompt plus vetted dynamic skills and temporary steering, a mockable toolkit, client capability gates, deterministic DuckDB computation, typed widgets, screening and regression evaluation. | This is Cortex's architecture, not verified architecture of the external trading MCP server. Its agent design is not a reason to replace WobbleBot's deterministic core or existing advisor architecture. |

[Trading with an agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)
documents long equities, options and crypto; tool families include eligibility,
review/place/cancel, approval status, lots, data and scanners. Native and external
catalogs differ. Native approval defaults ON; external MCP approval defaults OFF.
With approval enabled, the user manually places the proposed trade in Robinhood.
Those defaults do **not** change WobbleBot's approval firewall. Crypto transfer,
staking and lending are outside that documented agent execution surface.

[Evaluation engineering, June 9, 2026](https://robinhood.com/us/en/careers/blog/agent-eval-and-guardrail-studio-fixing-the-pain-of-designing-ai-evals/)
describes atomic trace-level criteria, human-calibrated graders, distinguishing
agent defects from grader defects, and versioned evaluation history. This is
particularly relevant to G4's repaired availability/judgment and fixture-version
contracts; it does not validate any of our models or campaign results.

No complete verified schemas, version guarantees, rate limits, idempotency/retry
contract, webhooks, paper sandbox, immutable exportable agent audit log or public
Agent Apps SDK were located in this research. Absence of found documentation is
not proof those capabilities do not exist. The separate
[crypto REST API](https://docs.robinhood.com/crypto/trading/) is not a universal
equities API.

## Kraken: official local tooling, with important authority limits

- [CLI announcement, March 11, 2026](https://blog.kraken.com/news/industry-news/announcing-the-kraken-cli)
  and the [official CLI page](https://www.kraken.com/kraken-cli) describe the shipped
  Rust CLI and local stdio MCP; the official maintained tool remains experimental.
  Latest release observed was [v0.4.1, August 7, 2026](https://github.com/krakenfx/kraken-cli/releases/tag/v0.4.1),
  with audit context and published binaries/checksums/signatures/provenance. Pin
  release and source commit before any later evaluation.
- The [README](https://github.com/krakenfx/kraken-cli) lists spot crypto, tokenized
  xStocks, forex, derivatives and Earn. `AAPLx` / `tokenized_asset` is not Kraken
  Securities ownership of real shares. Default MCP services include market,
  account, paper, workspace and feedback. Feedback transmits text to Kraken:
  omit it from a future research prototype. Do not network-expose local MCP;
  connected agents share the configured account's authority.
- [Repository instructions](https://github.com/krakenfx/kraken-cli/blob/main/AGENTS.md)
  document permission scopes and parameter/safety/error catalogs. Public data and
  paper use need no credentials; Spot and Futures credentials are separate.
  This brief grants no credential installation or account connection.
- [MCP server source](https://github.com/krakenfx/kraken-cli/blob/main/src/mcp/server.rs)
  checks caller-supplied `acknowledged=true` for guarded requests; this is **not
  human-approval verification**. `allow-dangerous` bypasses that guard. Audit JSONL
  on stderr includes allowlisted financial arguments, mode, caller, status and
  duration: restrict/redact logs instead of claiming financial details are absent.
- [Autonomy guidance](https://github.com/krakenfx/kraken-cli/blob/main/skills/kraken-autonomy-levels/SKILL.md)
  says position-size and frequency limits are not enforced by the CLI. Any later
  adapter must enforce policy outside the model and outside this acknowledgment.
- The reviewed [main client source](https://github.com/krakenfx/kraken-cli/blob/main/src/client.rs)
  retries connect/timeouts up to three times with exponential 500 ms base delay.
  Private POST timeout retries can occur even with `idempotent=false`; that flag
  prevents 5xx retries. **Inference:** an ambiguous timeout can duplicate effects;
  inspect the pinned lower-level retry implementation before live use. A wrapper's
  reconciliation policy cannot undo retries already performed beneath it.
  [Error-recovery guidance](https://github.com/krakenfx/kraken-cli/blob/main/skills/kraken-error-recovery/SKILL.md)
  recommends checking orders/trades after ambiguity.
- [AddOrder](https://docs.kraken.com/api-reference/trading/add-order) documents
  `validate=true` as validation, not execution/fill/slippage simulation. `cl_ord_id`
  uniqueness across open orders is not indefinite exactly-once transaction dedupe.
  [Dead-man cancellation](https://docs.kraken.com/api-reference/trading/cancel-all-orders-after-x)
  cancels orders, not filled positions; account scope matters. The documented
  refresh is 15–30 seconds with a 60-second timeout, not a generic restart policy.
- The [changelog](https://github.com/krakenfx/kraken-cli/blob/main/CHANGELOG.md)
  describes v0.4.0 paper workspaces, recordings/replay, graded experiments and
  exact-decimal fail-closed integrity. The
  [autoresearch example](https://github.com/krakenfx/kraken-cli/blob/main/examples/05-autoresearch-lab.md)
  describes sealed hypotheses, dataset hashes and evaluation sessions. These are
  research mechanics, not evidence of real fill fidelity or profitable strategies.
- Hosted Futures demo availability is unresolved:
  [testing-environment guidance, updated July 7](https://support.kraken.com/articles/360024809011-api-testing-environment-derivatives)
  contains a decommission notice despite older instructions, while the
  [advanced API FAQ](https://support.kraken.com/ca/articles/advanced-api-faq)
  still advertises it. Do not promise a hosted sandbox; documented local paper
  workflows remain a distinct capability.

These agentic tools do not establish a real-share Securities API contract.
[The equities evidence record](../planning/stage-9.0-design.md) retains that
separate contract gate and the disabled-by-default activation boundary.

## Proposed priorities and reentry criteria — not adopted

| Priority / idea | Existing WobbleBot overlap | Evidence needed before a separately authorized implementation |
| --- | --- | --- |
| P0: read-only capability adapter for account snapshots, positions, quotes, instrument eligibility and order intent | Existing ports, read-only observers, per-service grants and provider health. Avoid a new generic broker abstraction until a second verified contract needs it. | Pin a documented provider schema/version and explicit account/asset allowlists outside the model. Start with public data and isolated paper, without credentials or registered placement tools. Account data needs a separate privacy review and authorization. |
| P0: preview → approval → execution → reconciliation | ADR-049 already supplies immutable approval/claim semantics; ADR-026 guards withdrawal replay. Extend those mechanisms, do not create a parallel approval store. | Bind approval to account/instrument/side/size/type/price/expiry; changed intent invalidates it. Contract-test unknown submit outcome, partial fills and cancellation. Live financial execution remains separately authorized. |
| P1: deterministic decimal risk, spend and frequency limits | Existing deterministic grid/caps and sole Harvester transfer authority. | Demonstrate an unmet capability, keep research/execution authority separate, and inspect all provider retry layers before proposing live use. Never treat an agent acknowledgment as policy enforcement. |
| P1: central schema/capability registry | Existing typed ports, provider adapters and contract fixtures. | A real integration must justify shared machinery; use pinned schemas, mockable errors and upgrade tests. Distinguish crypto, tokenized assets, derivatives and real-share capabilities. |
| P1: restricted audit and trace-level evaluation | Existing command/outbox receipts, sanitized provenance, G4 repaired graders and versioned fixtures. | Define retention/access/redaction and source/freshness fields. Test injection, stale quotes, wrong account, duplicate effects, partial fills and revoked auth. Keep grader defects separate from agent defects. |
| P2: sealed replay experiments | Existing Auditor, outcome scoring and G3/G8 research gates. | Fixed hypotheses/thresholds/data hashes, unseen periods and fee/slippage stress. A research-only runner needs a deadline, budget, overlap lock, stale-data cutoff and stop condition. No change to live strategy from a paper score alone. |

Useful next research is a pinned, read-only contract comparison against the
existing ports, not installing either broker's agent integration. No proposal
above authorizes a new dependency, MCP server, scheduled agent, model-driven
execution, broker connection or change to financial authority. Existing G3/G8,
G9 and integration-specific adoption gates remain controlling.
