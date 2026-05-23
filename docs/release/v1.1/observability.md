# Observability — monitoring, alerting, backups

*Anomaly detection, retention policies, alternate notification fallbacks, metrics export, and backup verification. The /health page covers liveness today; entries here cover behavioral and operational visibility.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Anomaly detector daemon — cross-DB outlier watcher

**What:** a new long-running daemon (``cli/anomaly`` or similar)
that polls every project DB on a fast cadence and emits a
notification when it spots a statistical outlier against the
operator's own historical baseline. Not an LLM role — pure
deterministic Z-score / IQR detection over recent table state.

**Why high value:** the soak has surfaced 3-4 silent failure modes
(cli/live crashing without alert, cli/harvest + cli/advise +
cli/maintenance dying silently in the background, the
session-loss cap tripping without operator awareness for ~1.5h
on Day 5). The /health page catches *liveness* failures
(heartbeat-not-fresh); the anomaly detector catches *behavioral*
ones — the daemon is alive AND writing rows, but the rows it's
writing look wrong relative to the operator's normal pattern.

**Examples of what it'd catch:**
- ``trades.fee`` 5σ above the rolling mean → operator paid an
  unusual taker fee (maker→taker regime shift, or a fat-finger
  order_size that hit the book hard)
- ``orders`` cancel rate suddenly spiking → engine churn from
  a misconfigured cap or rapid market drift
- ``balance_entries.total`` dropping more than X% in one tick →
  could be a withdrawal we didn't expect, a wash from a bad
  fill, or a Kraken-side adjustment
- ``advisor_suggestions`` not appearing for >2 × normal cadence
  → cli/advise's heartbeat says it's alive but its work output
  has stalled (could be Ollama hanging without erroring)
- ``llm_calls.cost_usd`` for a single call >3σ above the daily
  mean → runaway prompt size, cost-gate not catching it
- ``transfer_proposals`` flipped from "hold band" to "surplus"
  unexpectedly → balance changed without a corresponding trade
  (deposit, manual transfer, refund — worth flagging)

**Implementation:** new ``services/anomaly_detector.py`` with a
registry of detectors (one per metric); each detector returns a
``(severity, description, supporting_metrics)`` tuple or None.
``cli/anomaly`` runs a poll loop, drives every detector against
the latest data, emits ``Notification`` rows (level=warning) for
each anomaly. Tuning: each detector takes a ``threshold_sigma``
config field; operator can adjust per detector. Heartbeats into
``daemon_heartbeats`` like every other daemon.

**Why deferred:** v1.0 freeze. Also needs a few weeks of baseline
data to calibrate Z-score thresholds; running on 5 days of soak
data produces too many false positives.

**Trigger:** post-v1.0 + ~30 days of accumulated baseline. Would
have caught every silent failure of the soak; lowest-hanging-fruit
observability upgrade after /health.

### Backup verification — restoration smoke test

**What:** monthly cli/maintenance task that opens the latest
backup file from each ``backup_dir`` source, runs
``PRAGMA integrity_check``, runs a representative SELECT against
each known table, and emits a notification on any failure.

**Why high value:** classic ops gotcha — untested backups are
not backups. cli/maintenance writes them via SQLite's online
``.backup`` API and prunes the old ones, but nothing ever opens
the backup files to confirm they're restorable. First time the
operator discovers a corrupted backup is the day they need it.

**Implementation:** new ``schedules.maintenance_backup_verify``
config key (default 7d cadence). New
``services/backuper.verify_backup(path)`` opens the file
read-only, runs ``PRAGMA integrity_check`` (must return "ok"),
queries ``sqlite_master`` for expected tables, runs a
``SELECT COUNT(*)`` against each. Returns a typed result
indicating "verified" / "failed_with_reason." New
cli/maintenance task picks the most recent backup per db_stem
and runs verify_backup; pushes a Notification on failure.

**Why deferred:** feature work; doesn't block v1.0. But should
land soon-after — cli/maintenance has been running since Day 1
and we've never verified its output.

**Trigger:** post-v1.0; one of the first cli/maintenance follow-ups.

### Data retention policy

**What:** explicit per-table retention windows; cli/maintenance
prunes old rows after the configured age, with the
already-established archive-then-delete discipline for audit
tables.

**Why high value:** today only ``price_snapshots`` gets pruned.
Every other table grows forever — ``orders``, ``trades``,
``advisor_suggestions``, ``applied_suggestions``,
``notifications``, ``conversation_turns``, ``llm_calls``,
``transfer_proposals``, ``transfer_results``, ``pending_commands``.
After a year of soak: at minimum 100s of MB in
``conversation_turns`` alone (cli/operator chatter), more in
notifications, more again in llm_calls. Eventually: slow
queries, big backups, disk-fill risk.

**Implementation:** per-table retention in the maintenance config
block (``maintenance.retention: { notifications: 90d,
conversation_turns: 30d, llm_calls: 365d, ... }``). For each:
archive-then-delete for audit tables (notifications →
``data/archive/notifications-{year}.csv``, llm_calls similar);
straight delete for ephemeral data. Audit tables (orders,
trades, applied_suggestions) probably KEEP-FOREVER — those are
the forensic ledger.

**Why deferred:** needs operator decision on which tables are
keep-forever vs prunable. Also: retention windows shouldn't
be guessed; should be tuned to observed growth rates after
~6 months of accumulated data.

**Trigger:** post-v1.0 + ~6 months of accumulated runtime data
so retention windows can be set against real growth curves.

### Daily summary email or Discord-DM

**What:** a `cli/daily-summary` daemon that produces "yesterday in
WobbleBot": fills, fees, harvester activity, LLM costs, any
warnings. Emailed (via SMTP env var) or DM'd via the existing
Discord adapter.

**Why deferred:** the operator's daily check during the soak will
reveal whether they want this pushed vs pulled. Pulling is the
existing model (web UI's status card); pushing is new behavior.

**Trigger:** operator says "I forgot to check the bot for two days
and now I'm catching up" — that's the signal pulling isn't enough.

### Per-cycle LLM call tracing

**What:** ``trace_id`` column on the ``llm_calls`` table grouping
all LLM calls within one cli/advise cycle (quant → risk → news
→ arbitrator) under a single UUID. Web UI ``/cost`` page gains
a "by cycle" toggle to drill into "which cycle was slow? which
role/provider was the bottleneck?"

**Why deferred:** observability nice-to-have, not blocking.
Adds a column + a UUID generator + a UI affordance.

**Trigger:** post-v1.0; pair with the per-LLM tracking work in
the advisor outcome evaluator entry — same migration touches
the ``llm_calls`` schema, so doing them together avoids two
separate migrations.

### Connectivity retry policy audit

**What:** documented sweep of retry-on-failure policy across
every external call site (Kraken REST, Ollama HTTP, all cloud
LLM HTTPs, RSS feeds, CryptoCompare). Output is
``docs/architecture/retry-policy.md`` documenting expected
behavior + identifying any inconsistencies + filing follow-up
work for missing retry coverage.

**Why deferred:** not a feature; documentation/refactor. The
soak hasn't surfaced retry-related defects (one finally-block
defect fixed Day 2, but that was missing try/except, not
missing retry).

**Trigger:** post-v1.0 cleanup phase OR next time a Kraken /
Ollama / cloud-LLM timeout incident surfaces uneven coverage.

### Disk space awareness in the anomaly detector

**What:** when the anomaly detector daemon ships (see Group 2),
extend it to monitor ``shutil.disk_usage(data_dir).percent``
and emit a warning notification when used > 80% / critical at
> 95%.

**Why deferred:** pairs with the anomaly detector entry; not
its own ship. SQLite + WAL + retention not in place yet means
this would fire prematurely under current free disk during
soak.

**Trigger:** bundles with the anomaly detector entry in Group 2.

### Ollama hang detection audit

**What:** audit Ollama HTTP timeout + cancellation handling
across cli/advise + cli/operator + (post-v1.0) cli/historian
to confirm that an Ollama process hanging without responding
doesn't block the daemon's event loop indefinitely.

**Why deferred:** probably-fine today but unverified.
``services/llm_retry.py`` wraps LLM calls with timeouts; we
believe the wrappers also cancel; an audit would prove it.

**Trigger:** post-v1.0; before Phase 9 (equities) raises LLM
call volume + Phase 9's new advisors compound the risk.

### Remote backup destinations (S3 / rclone / SFTP)

**What:** implementations of `services/backuper.BackupDestination`
Protocol. v1.0 only ships the local-FS variant.

**Why deferred:** Stage 8.2 explicitly scoped to local backups.
Off-host backup is operator infrastructure (their NAS already
replicates volumes presumably); codifying it in v1.0 would force
an opinion they may not share.

**Trigger:** operator's NAS has no volume replication and they
want belt-and-suspenders durability.

### Prometheus / metrics export

**What:** a `/metrics` endpoint on `cli/web` that exports counters
(orders placed, fills, fees paid, harvester proposals, LLM cost) in
Prometheus format.

**Why deferred:** the SQLite tables ARE the time-series store for
v1.0. Web UI's cost dashboard reads `llm_calls` directly; status
dashboard reads `orders` / `trades` directly.

**Trigger:** operator wires Grafana for cross-system visualization.

### PagerDuty / email / SMS alerting fallback

**What:** alternate notification destinations in case Discord is
down. The existing `notifications` SQLite table + forwarder pattern
extends cleanly — a new `PagerDutyNotifierAdapter` would consume
the same rows.

**Why deferred:** Discord has been adequate; multi-channel adds
complexity for a single-operator deployment.

**Trigger:** Discord outage causes the operator to miss a soak
window notification.

### Solo-operator incident runbook

**What:** a single `docs/release/v1.0-incident-runbook.md` (or
`docs/deploy/incident-runbook.md`) acting as the operator's
checklist + decision-tree when something bad happens. Sibling to
the existing `v1.0-soak-runbook.md` but scoped to "things broke,
now what" rather than "running a clean soak."

**Why this is NOT an "incident response process":** the 2026-05-23
security audit surfaced "no documented IR process" as an L3 gap.
The operator correctly flagged that "process" implies team
structure — on-call rotations, paging, war rooms, blameless
post-mortems. None of that applies to a solo project. The
SOLO-shaped version of the same concept is a runbook (one doc, no
organization implied) covering the realistic incident scenarios
for a single-operator personal trading tool.

**Scenarios worth covering:**

- **Suspected API key compromise** (Kraken read / trade / harvest
  key leaked, Discord token leaked, `WOBBLEBOT_WEB_SESSION_SECRET`
  leaked). Steps: stop the affected daemon, rotate the credential
  on Kraken/Discord/env, restart, audit recent activity in the
  affected DB for evidence of misuse.
- **Unexpected withdrawal observed in Kraken** (Harvester key
  abuse OR upstream Kraken issue). Steps: pause cli/harvest
  immediately, audit `transfer_results` for the txid, contact
  Kraken support if not operator-initiated, rotate the harvest
  key.
- **cli/web visible from internet unexpectedly** (port-forward
  rule, reverse-proxy misconfiguration). Steps: kill cli/web,
  audit `users.last_login_at` for unexpected logins, check
  reverse-proxy access logs, rotate session secret + invalidate
  all sessions, fix the exposure path.
- **gitleaks finding in git history** (a secret slipped in
  pre-the-pre-commit-hook era). Steps: rotate the leaked
  credential immediately, decide whether to filter-repo history
  vs document-and-move-on, force-push if rewriting.
- **Operator-personal info exposed** (a personal email or path
  somewhere in the codebase that's about to be / already is
  public). Steps: identify the file + line via the PII patterns,
  decide whether to filter-repo or accept, rotate any
  identity-bound credentials.
- **Bot behavior diverges from expected** (cap not enforced,
  order at wrong price, advisor recommending nonsense). Steps:
  freeze the engine (cli/live SIGINT), capture relevant DB
  state, compare against expected behavior, file as a
  soak-surfaced defect, fix, restart.

**Format:** each scenario gets the same 5-section template:
*Detection* (how would you notice), *Stop-the-bleeding* (immediate
action), *Assess* (what was the exposure), *Recover* (rotate +
restart), *Document* (so future-you knows). The whole doc fits in
one operator's-eye glance — checklist, not narrative.

**Why deferred from v1.0:** the bones already exist scattered
across the soak runbook ("Abort + restart procedure"), SECURITY.md
("Reporting concerns"), and v1.0-known-limitations.md.
Consolidating + expanding into one operator-facing doc is
real-but-bounded work (half a day) that's better done after v1.0
ships with the soak's actual lessons-learned baked in.

**Trigger:** post-v1.0 tag, OR any soak incident that would have
benefited from an existing playbook entry. The Day-2 thunderstorm
outage's recovery sequence ("manual cancel-on-Kraken + DELETE
grid_state + restart with fresh anchor") is the canonical
"this-belongs-in-a-runbook" example.

### Cost-honesty dashboard — bot's ROI against its own infrastructure

**What:** a new dashboard card (or `/cost` page extension) that
puts the bot's earnings side-by-side with its operating costs so
the operator can answer "is this thing actually profitable, or
am I subsidizing it?" at a glance. Two columns:

- **Earning side:** realized cycle PnL (sum from cycle_matcher),
  broken down by day / week / 30-day rolling window. Already
  computable from live.db.
- **Cost side:** trading fees (from `trades.fee`, already shown
  on the cost page) + LLM API spend (from `llm_calls.cost_usd`,
  already shown) + a new manual "infrastructure" line item the
  operator fills in once (NAS marginal power $/month, optional
  internet allocation). Stored as `cost_assumptions` rows in
  operator.db so the dashboard can render the math without
  guessing electricity rates.

The card surfaces three numbers:

1. **Gross earnings** — sum of cycle.net_pnl over the window.
2. **All-in cost** — trading fees + LLM API + operator-declared infra.
3. **Net vs cost** — the bottom line, with a red/green
   indicator at the $0 line.

Plus a one-line annualized projection: "at current pace, $X/year
net of all costs."

**Why this matters:** the v1.0 cost dashboard tracks LLM spend
and trading fees in isolation, but never asks the question that
matters most to the operator — *is the bot earning more than it
costs to run?* For a small-capital grid the answer can flip with
small config changes (cloud-LLM advisor on/off, spacing tweaks,
order size). Without a single number showing the verdict, the
operator has to do mental arithmetic across four pages every
time they want to know.

**Why the operator asked for this:** historical scar from a
prior crypto-mining attempt where electricity was costing more
than the mined coins were worth at the time. The bot is a
different shape of project (low marginal power on a NAS that's
already running, so the "am I underwater" question is mostly
academic *at current capital*), but the answer being "no, fine"
shouldn't be assumed — it should be measured and visible. And
when capital scales up, the question shifts from "am I underwater
on electricity" to "is the LLM cost or cloud-advisor cadence
eating my margin" — same dashboard answers both.

**Why deferred to v1.1:** at current $100 capital, infrastructure
cost (~$1-2/month marginal power, $0 LLM with Ollama default) is
~25-30% of earnings (~$3/month) — the margin is positive but
thin, and any operator who wants the verdict can compute it
manually from existing pages. The honesty-card is a quality-of-
life improvement, not a v1.0 gating concern. Becomes more useful
when (a) capital scales, (b) the operator toggles cloud LLM
advisors and wants to see margin impact in real time, or (c) a
config change appears to drop earnings and the operator wants
to A/B against cost without paging through history.

**Sketch:** small. One new `cost_assumptions` table (3-4 columns:
`name`, `monthly_usd`, `notes`, `updated_at`). One new Pydantic
schema. One new `/cost-honesty` route or extension to `/cost`.
Single template section. Settings page UI to edit the
assumptions inline (operator wouldn't want to hand-edit YAML
for this). Half-day of work end-to-end.

**Trigger:** any moment where the operator catches themselves
manually summing fees-plus-LLM-plus-power and dividing by
realized PnL to verify the strategy is still net-positive. Or
when scaling capital, since the answer shifts non-linearly
(earnings scale with capital, infra cost stays roughly flat).
