# Operator UX — Discord, web UI, CLI ergonomics

*Operator-facing improvements across all surfaces. The web UI is the largest cluster (Stage 7 substrate), with Discord, `cli/recalibrate`, daemon orchestration, and hosting topology rounding out the list.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Multi-coin defaults in `cli/recalibrate`

**What:** `cli/recalibrate --target-balance N` currently scales
USD knobs proportionally. A multi-coin variant would let the
operator say "give BTC 50% of the budget, ETH 30%, DOGE 20%" and
have recalibrate honor the split.

**Why deferred:** v1.0 recalibrate snaps per-coin
`order_size_usd` to the same scale factor. The single-knob model
is sufficient for the operator's current workflow.

**Trigger:** operator runs cli/live with multiple coins post-tag
and finds equal-weighting wrong for their portfolio.

### Discord command shortcuts

**What:** typed slash commands (`/pause BTC`, `/resume`, `/stop`,
`/status`) alongside the conversational LLM path. The conversational
path already handles these; shortcuts would skip the LLM parse for
the common cases.

**Why deferred:** Stage 5.3 wired the LLM intent parser; shortcuts
would be a parallel path. v1.0's conversational path is honest about
its latency (Ollama parse takes 1-3s); shortcuts trade ergonomics
for that delay.

**Trigger:** operator finds themselves typing the same phrases
repeatedly during the soak.

### Web UI per-entity action buttons (generic "decide + audit" pattern)

**What:** add commit-vs-reject buttons directly to the persisted
review queues in the web UI. The backend pattern is generic —
each per-entity row gets a positive-action button + a negative
`Reject` button, both routing through `pending_commands` per
ADR-013 with the existing two-click confirm page in between.

The positive-action label varies per surface to match the
project's existing domain vocabulary (the table name + the
storage model + the CLI verb all align with the button label):

| Surface | Positive | Storage model | CLI verb |
|---|---|---|---|
| Advisor suggestion | **Apply** | `AppliedSuggestion` | `cli/apply --commit` |
| Harvester proposal | **Execute** | `TransferResult` | `cli/harvest --execute` |
| Pending command (review queue) | **Approve** | `pending_commands.status='approved'` | (none — operator-only) |
| Notification | **Acknowledge** | `notifications.forwarded=1` | (none — passive marker) |

**Reject is consistent across all surfaces** — same negative verb
everywhere because "don't do this" has the same semantics in
every domain.

**Why per-domain positive verbs (not just "Accept"):** the
button label is the first thing the operator reads; mismatch
between button label and the audit row it creates is real
friction ("I clicked Accept, why does the audit log say
applied_at?"). Apply/Execute/Approve are already the vocabulary
operators see in CLI output, logs, and the existing storage
models — the buttons should reuse those names.

**ADR-002 firewall stays intact.** Apply/Execute/Approve don't
shortcut the confirm gate; they replace the CLI's audit row
with a web-originated one (`channel_id='web'`) and use the
existing two-click confirm page. cli/live's
`WHERE status='approved'` poll remains the only path from intent
to engine.

**Today's friction the buttons remove:**
- Advisor suggestion review: today is
  `tools/show_suggestions.py` to read, `cli/apply --commit
  --recommendation-id <id>` to apply. One-click would close
  the loop in the web UI.
- Harvester proposal execution: today is
  `tools/show_proposals.py` to read, switch to a terminal, run
  `cli/harvest --execute <proposal-id>`. One-click would unify
  the workflow.
- Pending command rejection: today the only path is wait for
  TTL expiry or react ❌ on Discord. With cli/operator
  disabled (Discord broken), the operator has no way to reject
  a pending row except wait. Web-side Reject closes the gap.
- Notification acknowledgment: with cli/operator off,
  notifications never get `forwarded=1`. Acknowledge gives the
  operator a manual mark-as-seen.

**Why deferred:** Stage 7's web UI scoped to read-mostly + engine-
control mutations (pause / resume / stop). Stage 7.5 closed Phase
7 explicitly without per-entity action buttons because the
review-via-CLI path (`tools/show_*` + `cli/apply`) was working.
Adding new mutation surfaces is feature work; v1.0 is in
documentation freeze per stage-8.4-design.md decision 3.

**Trigger:** surfaced during the v1.0 soak Day 2 (2026-05-20)
when the operator was weighing whether to flip auto-apply for
the soak. The CLI roundtrip (`tools/show_suggestions.py` to
read, `cli/apply --commit --recommendation-id <id>` to apply) is
real friction relative to one-click web actions. Pattern extends
naturally to other persisted-then-reviewed entities the operator
works with daily.

### Web UI read-views the operator wishes existed

**What:** soak-derived. The web UI shipped status / cost / advisor
/ harvester / news / audit views; the operator may find specific
information they want at a glance that isn't there.

**Why deferred:** speculative. Stage 7's view set was designed
against ADR-016's "read-mostly + ADR-013-firewalled mutations"
charter and the use cases known at the time.

**Trigger:** operator names a specific question they can't answer
from the current views.

### Status card per-order delta column

**What:** add a `delta` column to the open-orders table on the
status card showing each order's distance from current market
price as a signed percentage — e.g. a BUY at $76,570 with current
BTC at $77,891 renders `+1.73%` (current is 1.73% above the
order, so the order needs price to fall by that much to fill).
Same data the operator computes mentally today from the current-
price line + each row's price; the column does the arithmetic
inline.

**Why deferred:** soak Day 3 (2026-05-20) shipped the current-
price line itself, which closed the operator's primary "what are
these orders waiting on?" gap. The per-row delta is a UX polish
on top of that — useful when the grid has many orders but the
primary information need is already met. v1.0 is in documentation
freeze per stage-8.4-design.md decision 3; adding new column +
template logic during soak is feature work, not defect work.

**How:** template-only change. ``StatusSnapshot.current_prices``
already carries the per-symbol market price (Stage 8.4.E
`b5cedfe`). Add a column to ``_status_card.html``'s open-orders
table that computes ``(current - order_price) / order_price *
100`` when ``snapshot.current_prices.get(o.symbol)`` is set, falls
back to `—` when not. Sign convention: positive delta = order is
"underwater" from current (BUY needs price to drop, SELL needs
price to rise to reach it). Worth a unit test on the formatting
edge cases (no current price, zero delta, large negative).

**Trigger:** operator surfacing during soak Day 3 status check
that the current-price line answers "what's BTC at?" but doesn't
answer "how close is each order to firing?" — the delta column
closes the latter.

### Footer: "update available" indicator

**What:** the footer (Stage 8.4.E soak Day 4) shows
``WobbleBot v{{ app_version }}``. v1.1 adds a small icon next to
the version when a newer GitHub release exists, indicating an
upgrade is available. Click → link to the release notes / GitHub
releases page.

**Why deferred:** no v1.0 tag exists yet. Pre-1.0 the feature has
nothing to compare against — every check would return "no
update." Lands naturally with the v1.0 tag in the post-soak
release ceremony (Stage 8.4.F).

**How:**
- New ``services/release_checker.py`` polling
  ``https://api.github.com/repos/CarlDog/wobblebot/releases/latest``
  on a long cadence (proposal: hourly). Caches the result in
  ``operator.db`` or in-process state.
- Background task in ``cli/web`` (or piggyback on
  ``cli/maintenance``) refreshes the cache.
- Footer placement (operator-decided 2026-05-21):
  **line 2, after the repo link, separated by " · "**.
  Lays out as:
  ``github.com/CarlDog/wobblebot · ↑ Update available``
  When no update available, the second item is omitted entirely
  (line 2 stays as just the repo link). Indicator visible only
  when there's actually news to report.
- Operator-facing: hover tooltip shows the new version + release
  notes link; click goes to
  ``https://github.com/CarlDog/wobblebot/releases``.

**Privacy + rate-limit notes:**
- Polling from the **server**, not from the browser — keeps the
  operator's dashboard activity from leaking to GitHub.
- Hourly poll is well under GitHub's 60/hr unauthenticated
  limit; could go to 6 hours for an even safer margin.
- Cache survives ``cli/web`` bounces (operator.db row).
- Settings knob to disable the check entirely for operators who
  prefer no outbound calls (default: enabled).

**Trigger:** v1.0 tag ships; operator wants to know when v1.1
work is available without manually checking GitHub.

### State-aware per-symbol pause/resume buttons

**What:** the status card's per-symbol pause/resume icons
(Stage 8.4.E soak Day 4) currently show BOTH actions for every
symbol regardless of current state. Operator clicks the action
they want; engine handles "tried to pause an already-paused
symbol" idempotently. v1.1 closes the loop: render only the
contextually relevant action (▶ resume on paused symbols, ⏸
pause on active ones), plus a subtle dimmed-row visual for
paused symbols.

**Why deferred:** ``cli/live``'s pause state lives in-memory in
the ``GridEngine`` instance. ``cli/web`` has no read access — it
can only write into ``pending_commands`` and watch the row
status flip. The state-visibility gap is already documented as
a v1.0 limitation: "``cli/operator``'s stub engine doesn't see
``cli/live``'s in-memory pause state; ``StatusQuery`` reports
all symbols as ``active``."

Closing the loop requires the engine to publish its pause state
to a table ``cli/web`` can read — that's the v1.1 work.

**How:**
- New ``engine_state`` table in ``operator.db`` with
  ``(symbol_base, symbol_quote, paused, paused_at)`` columns.
- ``cli/live`` writes a row per symbol on pause/resume command
  execution (after the existing engine.pause_symbol /
  resume_symbol call succeeds).
- ``StoragePort`` gains ``get_paused_symbols()`` async method.
- ``StatusSnapshot`` gains ``paused_symbols: set[Symbol]``.
- Template renders one icon based on the symbol's paused state
  + adds a ``.symbol-section.paused`` class for visual dimming
  + a "(paused)" label next to the symbol name.

**Trigger:** pairs naturally with the operator-initiated
re-anchor mechanism (also v1.1, also needs engine -> web state
visibility). Both can ship together — the ``engine_state``
table doubles as the read substrate for any future
engine-state surface in the web UI.

### Notifications surface — server-side read state + deep linking

**What:** v1.0 ships a bell icon (navbar far-right) + ``/notifications``
read-only page + browser-local "last seen" badge logic (Stage
8.4.E soak Day 4). Two follow-up layers complete the design:

1. **Server-side read state.** Browser ``localStorage`` works for
   single-operator-single-device v1.0, but doesn't sync across
   devices and can't drive a "mark all read" mutation. v1.1 adds:
   - New ``read_at TEXT`` column on the ``notifications`` table
     (schema migration).
   - ``POST /notifications/{id}/read`` — marks one notification
     read.
   - ``POST /notifications/read-all`` — marks every unread
     notification read for the current user.
   - Bell badge counts server-side unread; survives across
     devices.
   - The Phase 5 ``forwarded`` boolean stays Discord-specific;
     read state is its own concern (the two surfaces can be
     independently consumed without fighting).

2. **Deep linking per notification type.** Today every
   notification is a passive log line — operator reads, dismisses.
   Future: each ``Notification`` carries a ``link`` field (or
   computed from ``context_json``) that takes the operator
   directly to the related view:
   - fill notification -> ``/dashboard`` scrolled to that trade
   - cap-trip warning -> ``/dashboard`` with the relevant card
     highlighted
   - harvester proposal -> ``/harvester``
   - withdrawal-executed -> ``/audit`` with the proposal row
     anchored
   Implementation: extend ``Notification`` (and the SQLite schema)
   with an optional ``link_path`` / ``link_query`` pair. Emitting
   code populates per call site. Template renders the
   notification title as a link when set.

**Why deferred:** the read-state layer needs a schema migration
mid-soak — exactly the kind of thing the soak discipline defers.
Deep linking needs a per-notification-type design pass (what
links where, what query params survive across page nav, what
happens when the linked entity has been pruned). Browser-local
``last seen`` covers the v1.0 case acceptably; the bell badge
clears on visit, the operator sees the notification list, the
loop closes.

**Trigger:** soak Day 4 (2026-05-21) confirmed the bell + page
work; operator pre-requested both follow-ups as natural
extensions. Implementation order: schema migration + read state
endpoints first (since deep linking needs the row identity to be
addressable in the URL); deep linking second (more design
discussion needed).

### Re-anchor banner — action button + snooze

**What:** the dashboard now shows colored re-anchor
recommendation banners when the drift + age heuristic suggests
the grid has walked away from market (soak Day 4 add). v1.0
ships info-only — banner displays the recommendation, operator
performs the manual SIGINT + DELETE + restart sequence per the
soak runbook. v1.1 closes the loop with two button additions:

1. **"Re-anchor BTC/USD" button** (primary action) — invokes
   the operator-initiated re-anchor mechanism (separate v1.1
   candidate). Routes through `pending_commands` per ADR-013;
   confirm page → status='approved' → cli/live polls and
   executes the rotation. ADR-002 firewall intact.

2. **"Snooze 24h" button** (secondary action) — suppresses the
   banner for the symbol until the snooze expiry. Storage
   layer: new `reanchor_snoozes` table in `operator.db` with
   `(symbol_base, symbol_quote, snoozed_until)` columns;
   `_load_reanchor_recommendations` filters out symbols with an
   unexpired snooze entry. Snooze survives daemon bounces.

**Auto-cancellation policy — explicitly rejected.** Two technical
paths considered and rejected as banner-replacement during soak
Day 4 design discussion:

- **Kraken `expiretm`** (engine sets order expiration at
  AddOrder time). Pro: zero runtime cost; Kraken does the work.
  Con: order silently disappears; engine doesn't know until
  reconciler runs, so grid silently shrinks with no
  replacement logic.
- **Wobblebot-side stale cancel** (`cli/live` checks each tick,
  cancels orders > N days). Pro: under engine control. Con:
  same "grid shrinks" issue without paired re-placement logic.

Both fight ADR-006's "engine stays parked when offside" safety
posture. The cleaner path — and the one wobblebot is taking — is
*recommend, don't auto-execute*: surface the misalignment as a
banner, let the operator decide to re-anchor.

**Why deferred:** the info-only banner ships in v1.0 because
it's pure UI (no engine side effects). The action button
requires the operator-initiated re-anchor mechanism to exist
first; that's its own v1.1 item. Snooze adds a new
`operator.db` table — schema migration that's better-grouped
with other v1.1 storage changes than dropped in mid-soak.

**Trigger:** operator surfacing during soak Day 4 design
discussion that "auto-cancellation feels wrong; lean into
banner + action button instead." Shipping order matches the
dependency: re-anchor mechanism → action button → snooze.

### Status card recent-fills section enhancement

**What:** today the "last fill X ago" timestamp lives in the
card-meta header, separated from the "Recent fills" subsection
that displays the fill data itself. Move the freshness line down
into the Recent fills section where it conceptually belongs. Also
expand that section beyond a single timestamp:

- per-row "X ago" column on the fills table (apply
  `humanize_duration` to `(now - trade.executed_at)` for each
  row)
- subhead summary stats — count of fills in window, total cost
  / fees, possibly PnL since last anchor

**Why deferred:** the current single "last fill X ago" line in
card-meta does the minimum job — operator sees freshness at a
glance. Expanding to richer per-fill context is feature work,
and v1.0 is in documentation freeze. The `humanize_duration`
filter (Stage 8.4.E `b5cedfe`) is already the building block;
this entry queues its broader application.

**How:** template-only change to `_status_card.html`. Pull the
"last fill {{ X | humanize_duration }} ago ·" string out of
card-meta; add it as a sub-header above the recent fills table
together with summary stats; optionally add an "age" column to
the fills table itself using the same filter applied to
`(datetime.now(UTC) - trade.executed_at.dt).total_seconds()`.
Snapshot already carries `recent_trades` so the data is in hand.

**Trigger:** operator surfacing during soak Day 3 nightly check
that the card-meta is getting crowded with freshness signals
(refreshed-at, last-fill-X-ago, price-context) and the
recent-fills section is the natural home for fill-related
metadata. With more concurrent symbols (multi-coin status card
layout entry above), the meta line can't carry per-symbol fill
ages — the section-level home scales better.

### Status card multi-coin layout

**What:** the current-price line on the status card renders as
inline `<span>` ticker chips separated by margin
(`market-ticker` class). Works cleanly for 1–4 symbols; starts
wrapping awkwardly at 5–7; becomes a wall of text at 8+. As the
operator runs grids on more coins concurrently, the layout needs
to scale gracefully.

Two complementary paths to choose between when the time comes:

1. **CSS grid wrap** — minimal change, ~10 lines of CSS swapping
   inline-with-margin for `display: grid` with
   `grid-template-columns: repeat(auto-fit, minmax(120px, 1fr))`.
   Auto-wraps into a clean grid of price cells. Handles 1–20
   coins. Lowest disruption to existing UI.
2. **Per-symbol order grouping** — bigger refactor. Replace the
   flat open-orders table with per-symbol subsections, each
   headed by `=== BTC/USD ($77,891) ===` (current price baked
   into the subhead). Cleaner at any coin count and pairs the
   price with its orders contextually. Subsection headers also
   give the per-order delta column (above) a natural fit.

**Why deferred:** soak currently runs 1 coin (BTC/USD); the
existing inline layout works perfectly. The break-point is
~5 coins, and v1.0 won't see that. Designing for hypothetical
multi-coin width during documentation freeze would be premature.

**Trigger:** operator increases to 3+ concurrent coins post-soak,
or any time the status card starts wrapping mid-ticker on a
normal desktop window width.

### Discord response quality: data + presentation + model attribution — ✅ shipped in v1.0 (2026-05-24)

**Status:** ✅ Shipped in three commits 2026-05-24 during the soak.
All three parts landed:

1. **StatusQuery + similar query handlers read live.db** — shipped
   2026-05-23 as `a2dcbf1` (Discord StatusQuery reports honest
   engine state). Three stacked wiring gaps closed: cli/operator
   now opens observe.db for balance lookups, `active_symbols`
   threaded from `config.live.symbols`, `OperatorService` reads
   `today_realized_pnl` for session_pnl instead of hardcoded 0.
2. **QueryResult → embed rendering** — shipped 2026-05-24 as
   `227c327`. New `services/discord_embed_render.py` with one
   pure renderer per QueryResult variant (9 total) returning
   `send_embed` kwargs. Color-coded (StatusResult success / warning
   on paused symbols, HarvesterStatusResult by band, etc.) and
   overflow-capped at 10 entries per embed with "N more not shown"
   marker. 21 tests covering every variant + empty + overflow.
3. **Model attribution footer** — shipped 2026-05-24 as `c4cd95b`.
   Query embeds footer with `parsed by <model_name>` sourced from
   `operator_cfg.assistant.model` (no AssistantPort signature
   change needed — the configured model is the parsing model).
   Bonus from the same commit: ack-reactions on inbound messages
   (👀 ACK_EMOJI on parsed intent, ⚠️ WARN_EMOJI on Unparseable)
   leveraging the previously-unused `add_reactions` permission.

Together: 27 new unit tests, mypy clean, pylint 10.00/10. The
original entry body is retained below for historical context.

---

**What:** three related improvements to how cli/operator's Discord
responses look + what they contain. Discoverable as one coherent
slice because each one touches the same OperatorService response
path:

1. **StatusQuery + similar query handlers read live.db.** Today
   the handlers in ``services/operator_service.py`` answer from
   cli/operator's stub-engine state — which is a placeholder. Real
   trading state (open symbols, balance, session PnL) lives in
   cli/live's in-memory engine, which cli/operator can't see across
   process boundaries. The persisted snapshot in ``live.db`` is
   the right source: same DB the web ``routes/status.py`` reads.
   The Discord status response would surface real numbers instead
   of zeros and empty lists.

2. **QueryResult → embed rendering.** Today the response is a raw
   JSON blob in a single Discord code block. Switching to a
   field-per-attribute embed (like the existing fill notifications)
   gives the operator a glanceable table: each field on its own
   line, color-coded for level, with consistent number/duration
   formatting. The notification embeds already do this; the query
   responses should match.

3. **Model attribution footer.** Every conversational response
   should carry the model that generated it (e.g.
   ``model: claude-sonnet-4-6`` or ``model: phi4:14b-q8_0``) in
   the Discord embed footer. The ``llm_calls`` ledger already
   records this per call (Phase 6 ADR-014); the Discord embed
   just needs to surface it. Without attribution, the operator
   can't tell if a slow or weird response was Ollama vs a cloud
   provider — useful both for debugging response quality and for
   verifying the cost gate's failover behavior.

**Why high value:** the Discord surface is the operator's most
direct interaction with the bot. Today the conversational replies
feel half-baked compared to the structured-data notifications.
Closing this gap turns "operator-can-query-from-anywhere" into a
first-class feature instead of a "look, it technically works"
demo.

**Implementation:** medium-sized slice. (1) needs the StatusQuery
handler (and a few siblings — BalanceQuery, OpenOrdersQuery if
they have the same staleness) to take a ``live_storage`` port and
read from it instead of the stub engine. (2) needs a new
``services/discord_embed_render.py`` helper that walks any
``QueryResult`` variant and produces the embed structure cli/live's
fills emit; ``cli/operator``'s flow handlers swap from "send
JSON-blob message" to "send embed via render helper". (3) needs
``AssistantPort.parse`` to return the ``model_id`` (already on
``AssistantResult`` for cloud providers; same field on Ollama
provider should be honored), and the embed helper threads it into
the footer.

**Why deferred:** every part is feature work, not bug fix. The
Discord surface is functional; the data quality + presentation
gaps are operator-visible but not blocking. Soak Day 5 surfaced
the empty-fields issue, but the web ``/dashboard`` shows the
correct data, so the operator isn't flying blind.

**Trigger:** post-soak, packaged together so the Discord response
posture changes once (predictable for operators) rather than across
three separate commits that each shift the embed format slightly.

### Operator command catalog: single source of truth across prompt + code

**What:** today the catalog of available operator commands and
queries is defined in two places that have to be kept in sync by
hand:

1. `config/prompts/operator.md` — the system prompt loaded into
   every LLM parse. The "Available command kinds" and "Available
   query kinds" sections enumerate every parseable intent (pause /
   resume / pause_all / resume_all / cancel_open_orders / stop on
   the command side; status / open_orders / recent_fills /
   recent_suggestions / recent_news / harvester_status /
   recent_proposals / grid_config / help on the query side). The
   LLM uses this list to ground parsing — anything outside it
   should become `{"kind": "unparseable"}`.
2. `services/operator_service.py` — the module-level
   `_HELP_ENTRIES` tuple (~15 `HelpEntry` rows) that backs
   `HelpQuery → HelpResult`. The operator asks "help" or "what
   can you do" and the Discord embed lists these.

These two lists describe the same catalog but in different
formats — the prompt is human-readable Markdown bullets with JSON
shape examples; `_HELP_ENTRIES` is a Pydantic tuple. They have
drifted in the past (add a new query, update the code, forget the
prompt → LLM treats the new query as out-of-catalog and emits
`unparseable`). The reverse direction is just as bad (prompt
mentions a capability that doesn't exist; LLM happily emits the
intent shape; dispatch dies on `match _: raise`).

**Why high value:** the LLM is the only thing between operator
natural language and the parsed intent. If its catalog drifts
from the code's catalog, parses silently fail in either direction.
The cost is operator surprise — "I asked for X and it told me it
couldn't parse" or "it parsed but nothing happened" — not data
corruption, but the kind of friction that erodes trust in the
operator surface.

**Implementation options (pick one):**

1. **Derive prompt from code.** Treat `_HELP_ENTRIES` (or a
   richer descriptor including JSON examples) as the source of
   truth; the prompt becomes a Jinja template that interpolates
   the catalog at load time. `config/prompts.load_prompt` already
   has the frontmatter+Markdown loader hook; extend it to accept
   a context dict for rendering. Pro: code-first, prompt drift
   is impossible. Con: prompts are touched by operators (per
   `wobblebot.config.prompts.load_prompt`'s "operators edit
   freely" comment), so any operator-tunable wording (system
   role, constraints, output schema) has to stay editable.
2. **Code derives from prompt.** Parse the catalog sections out
   of `operator.md` into the `_HELP_ENTRIES` tuple at module
   import time. Pro: prompt stays human-edited. Con: relies on
   parser robustness against prompt edits; an operator
   reformatting a bullet could break catalog loading at startup.
3. **Schema-drift test (lowest risk).** A unit test that parses
   the prompt's command + query lists and asserts they match
   `_HELP_ENTRIES`. Doesn't fix the drift mechanically, but
   catches it in CI / pre-commit before it ships. Pro: tiny
   change, no runtime impact, prompt and code both stay where
   they are. Con: doesn't prevent the gap window between code
   change and prompt edit on the same branch.

Recommended starting point: option 3 (test) since it's the
smallest change and converts "silently drifts" into "loudly
fails on the next test run". Promote to option 1 if the
test-failure rate makes the manual sync feel obviously wrong.

**Why deferred:** the existing drift is recoverable per-incident
(operator notices, files a bug, both lists get updated). The
catalog has been stable across the last six stages — adds happen
once per phase, not once per week — so the practical drift rate
is low. Worth queuing for v1.1 but not blocking v1.0.

**Trigger:** next time the operator catalog gains a new command
or query (most likely candidates: a `re-anchor` command per the
related v1.1 entry, or a `cost_summary` query). Adding two new
catalog entries at once is the moment to wire up the
single-source-of-truth pattern instead of doing it by hand a
third time.

### One-command daemon orchestrator (`cli/up` wrapper)

**What:** a new operator entry point (likely `cli/up` or
`tools/up.py`) that spins up the appropriate set of daemons —
seven for live mode (`cli/up live`), seven for shadow mode
(`cli/up shadow`) — under a single supervisor process. Built on
top of an existing supervisor (recommended: `honcho`, Python's
Foreman equivalent, ~500 lines, well-maintained, single dep) +
two `Procfile` definitions (`Procfile.live`, `Procfile.shadow`).
The thin `cli/up` Python wrapper handles pre-launch validation
(env-var checks, stale-process cleanup, preflight against
Kraken) before exec'ing into `honcho`.

**Why high-value:** the manual seven-terminal launch is real
friction. Surfaced by operator on 2026-05-20 (soak Day 2) after
the day's two crash-recovery sequences each required opening 7
PowerShell windows + activating venv in each + typing commands
in the right order. Twice in one day exposed three concrete
costs:

- "Did I actually launch all of them?" — we hit this twice;
  process-inventory audit revealed missing daemons.
- "Did I launch them in the right order?" — cli/live should be
  last so reconciler runs against everything else's quiet state.
- "Are they all on the new code?" — restart-everything-after-fix
  scenarios are common during active development.

A wrapper collapses all three concerns into one verb. Two
flavors (`up live` vs `up shadow`) makes the spot-vs-paper
distinction loud + unmissable at launch time.

**Tradeoffs to design through:**
- Per-daemon terminal visibility is lost — output gets merged.
  Honcho's colored prefixed output is a partial mitigation;
  operator can still tail individual data/*.db tables for
  per-daemon state. Some operators may prefer one-window-per-
  daemon and just want the wrapper to spawn detached terminals
  (PowerShell `Start-Process` variant).
- Process supervision policy questions: one daemon dies — do we
  kill the others (honcho default) or just log and continue
  (production-grade)? Probably configurable.
- Per-daemon restart becomes harder; operator who just wants to
  bounce cli/live has to leave the wrapper.

**Why deferred:** v1.0 ships the manual-launch model documented
in the soak runbook. Adding a wrapper is feature work; v1.0 is
in documentation freeze. The wrapper is dev-time convenience —
production deployment on Synology would use systemd or Portainer
regardless.

**Trigger:** post-v1.0, before any soak that requires restarting
the full daemon set frequently. Realistic v1.1 candidate; ~2-3
hours of focused work (Procfiles + thin `cli/up` Python wrapper
+ env-var preflight). Honcho is the single new dep.

### Always-on hosting topology — decouple from operator laptop

**What:** move the 7-daemon runtime off the operator's primary
machine to something that's always on and isolated from desk-side
power / network failures. Operator-surfaced 2026-05-22 after the
Day-2 thunderstorm + power outage cost ~12 hours of soak time
and stranded 3 open BUYs on Kraken until the operator got home.

**Why the question exists:** the bot's value comes from being
always running. Any local failure mode (UPS-defeating outage,
ISP drop, Windows update at 3 AM, laptop sleep, ...) is downtime,
and grid bots specifically lose money during downtime because
counter-fills don't get placed and price drift can leave the grid
offside without recovery.

**Candidates to weigh** (no leading choice committed; that's the
ADR's job when the time comes):

- **Synology NAS via Portainer (operator's existing Docker host
  fleet).** Most natural fit — operator already runs Plex,
  Portainer, Servarr stack, multiple MCP servers (plex-mcp,
  portainer-mcp, downloader-mcp, etc.) as Portainer-managed Docker
  stacks. Reuses an established operational pattern: same
  Portainer UI, same backup posture (Synology Hyper Backup), same
  Tailscale/LAN access pattern, same upgrade workflow. Wobblebot
  would join the fleet as another stack. Hardware already paid
  for; ongoing cost $0. Trade-off: NAS CPU is typically modest
  (Celeron / Atom-class on consumer Synology), so the Ollama
  loadout needs sizing — the advisor's `phi4:14b-q8_0` may need
  to drop to a lighter model OR keep Ollama on a separate host.
- **Raspberry Pi on UPS (operator has one sitting around).**
  Pi 5 with 8 GB RAM handles 7 Python daemons + a 7B-quantized
  Ollama model comfortably. Headless, Wireguard/Tailscale for
  operator access, ADR-016 reverse-proxy posture extends cleanly.
  Hardware already on-hand; ~$100 UPS. Adds a new device to
  maintain (separate from the NAS fleet).
- **Cloud VPS (DigitalOcean / Hetzner / Fly.io).** ~$5-12/month
  basic droplet. Removes power + ISP failure modes entirely;
  Kraken latency typically better than residential. Trade-offs:
  shifts secrets onto a 24/7 attack surface (Kraken keys,
  Discord token, web session secret); Kraken API key IP
  allowlists must rotate to cloud IP; needs offsite backup
  strategy for SQLite DBs (cli/maintenance currently writes
  local files); adds CI/CD complexity if updates aren't done
  by SSH.

**Counter-argument worth keeping in mind:** the Day-2 outage
**exposed a real engine defect** (`e2b6cfc` finally-block fix).
A cloud or always-on host would have masked it indefinitely.
There's value in keeping the soak environment representative of
"things will fail." Argues for not migrating *during* soak,
regardless of which destination is picked.

**Implementation (Synology path — most likely):** package the 7
daemons as a docker-compose stack matching the operator's
existing Portainer-managed stack pattern. Each daemon gets its
own service; shared volume on a Synology share for `data/` so
SQLite paths line up. Secrets via Portainer's stack-env or a
mounted `.env` file with restricted permissions. cli/web behind
the NAS's existing reverse proxy (or Caddy/Traefik service in
the stack). Operator access via Tailscale to the NAS (already
configured) → cli/web on internal address. Kraken key IP
allowlist points at the NAS's WAN IP. Ollama either co-located
in the stack with a lighter model or kept on a separate
host that the advisor calls over the LAN.

**Implementation (Pi path):** docker-compose stack on Pi OS,
shared volume for `data/`, secrets via `env_file`,
Tailscale-only access. Same wobblebot architecture, different
host.

**Implementation (cloud path):** same docker-compose stack on
a VPS, with secrets via the platform's secret manager (Doppler,
1Password Connect, DO App Platform secrets). Offsite backup via
`cli/maintenance` backup hooks pointed at S3 / B2 / rclone-to-NAS.
Reserved static IP for the Kraken allowlist. cli/web behind
Caddy/Traefik with Let's Encrypt TLS.

**Why deferred:** v1.0 architecture assumes operator-managed
local execution (ADR-016 explicitly says "operator's reverse
proxy fronts the LAN"). A hosting topology decision deserves
its own ADR and a deliberate stage post-v1.0. Premature
deployment work undermines the soak's purpose.

**Trigger:** post-v1.0 tag. First decision the future ADR has
to make: where Ollama lives (on the same host as wobblebot vs a
separate one), since that drives the host's CPU/memory sizing
requirement more than anything else.

### cli/init setup wizard

**What:** interactive ``cli/init`` that walks a new user through
.env setup, key creation reminders, settings.yml copy from
example, and a first cli/preflight invocation.

**Why deferred:** solo project today; no contributors. The
phase-end-audit rule explicitly cautions against speculative
contributor-facing tooling. Adds maintenance surface for zero
current users.

**Trigger:** when external contributors actually materialize OR
when the operator deploys WobbleBot on a fresh machine (Pi /
Synology / VPS) for the first time and notices the bar to entry.

### Session cookie keyed by user.id, not username

**What:** today's web session cookie stores
``session["username"] = user.username`` (the literal string).
``current_user`` looks the user up by that string each request via
``get_user_by_username``. Switch to storing
``session["user_id"] = user.id`` (the integer PK) and look up via
``get_user_by_id``.

**Why high value:** username changes (operator renames their own
account, future multi-user admin renames another user) silently
invalidate every active session for that user. The 2026-05-23
``cyeagerlmt → CarlDog`` rename hit this exact pattern: the
session cookie's stale ``username`` value caused a redirect loop
between ``/dashboard`` and ``/auth/login`` until ``current_user``
was patched to clear stale sessions (commit ``f4d707a``).

The clear-on-stale-lookup patch covers the broken case correctly
(stale sessions get cleared instead of looping). But the
ARCHITECTURALLY cleaner answer is to key sessions on the immutable
user.id from the start. user.id never changes; renames don't
invalidate cookies; deleted-user case is unchanged (look up by id
returns None, clear session).

**Why deferred from v1.0:** the migration question. Switching the
cookie schema would silently invalidate every existing session
cookie (rows that say ``{"username": "CarlDog"}`` no longer
match the new lookup). Operators on the running system would all
need to sign in again. Not a regression — a one-time visible
inconvenience — but worth a deliberate "we're going to invalidate
your session on the next bounce" pre-announcement rather than a
surprise. Easy work, just deserves a coordinated landing.

**Implementation:**
- Add ``StoragePort.get_user_by_id(user_id: int) -> User | None``
  if not already present (matches the existing
  ``get_user_by_username`` shape).
- ``current_user`` reads ``session.get("user_id")``, looks up by
  id. Falls through to clearing the session on None (same
  defensive pattern as the current rename-hotfix).
- ``/auth/login`` POST sets ``session["user_id"] = user.id`` on
  successful auth. Stop setting ``session["username"]``.
- Migration: on the FIRST request after deploy, ``current_user``
  sees ``session["username"]`` set but no ``session["user_id"]``.
  Either (a) look up the user by the stale username one last time
  and upgrade the cookie to user_id, or (b) just clear the
  session and force a re-login. Path (a) is operator-friendlier;
  path (b) is simpler and more defensible (any stale username
  would already be invalidated by the operator's intent to do the
  rename in the first place).

**Trigger:** any v1.1 release that's announced ahead of time, OR
the moment multi-user support starts being a serious concern (the
broken-sessions-on-rename UX is more visible with more users).

### Multi-factor authentication (TOTP) for the web UI

**What:** add TOTP-based MFA on top of the existing
username/password login. Operator scans a QR code at first
enrollment; subsequent logins require the password + a 6-digit
code from an authenticator app (Authy, Google Authenticator,
1Password).

**Why deferred from v1.0:** the single biggest gap vs OWASP ASVS
L3 (v4.2.4) for a financial app. Solo-operator behind LAN-only
HTTPS makes the practical risk LOW, but it's the canonical
financial-application control and surfaces in every audit.
Documented as a deliberate L3 gap in the security audit (2026-05-23).

**Becomes load-bearing when:**
- The operator account gets shared (a partner, a family member,
  a contractor)
- Multi-user web auth lands (separate v1.1 entry above; the two
  pair naturally)
- cli/web is ever exposed beyond LAN (port-forward, public
  domain — even with HTTPS, password-only auth on an internet-
  facing finance app is genuinely insufficient)

**Implementation outline:**
- New `pyotp` runtime dep (well-maintained, ~5 years old, MIT)
- New `users.totp_secret TEXT` column (additive schema migration;
  NULL = MFA not enrolled for that user)
- First-login enrollment flow: generate secret + QR code, store
  hashed-or-encrypted (HMAC the secret with session secret? or
  use a per-user encrypted column?), require operator to enter
  a code to confirm
- Login POST gains a `totp_code` field; gated when
  `users.totp_secret IS NOT NULL`
- Recovery codes (10x one-time-use codes shown at enrollment,
  hashed in DB) for the "I lost my phone" case
- `cli/web disable-mfa --username <name>` subcommand for the
  operator's break-glass path (requires console access — the
  same access that lets you read `.env`)

**Trigger:** any of the three "becomes load-bearing when"
conditions above. Until then: per-IP rate-limit + bcrypt cost 12
+ single-user-on-LAN is reasonable.

### Content-Security-Policy header

**What:** add a `Content-Security-Policy` response header to the
web UI's HTML responses, restricting where scripts/styles/images
can be loaded from. Defense-in-depth on top of Jinja2's autoescape
(which prevents most XSS at the template layer).

**Why deferred from v1.0:** L3-only ASVS requirement (v5.1.4); L2
considers autoescape + no `|safe` filters sufficient. Solo-operator
+ LAN-bound + no untrusted-user-input surface (the operator is the
only one filing input) makes the practical XSS risk low.

**Why high-value at v1.1:**
- Cheap to ship (~10 lines of middleware code + the actual policy
  string)
- Provides a hard wall against any future XSS that slips past
  autoescape (e.g., a vulnerable Jinja2 release, a forgotten
  `|safe` in a future template)
- Required by every serious financial-app deployment standard

**Suggested initial policy:**
```
default-src 'self';
script-src 'self' 'sha256-<htmx-hash>';
style-src 'self' 'unsafe-inline';
img-src 'self' data:;
connect-src 'self';
frame-ancestors 'none';
form-action 'self';
base-uri 'self';
```

`'unsafe-inline'` on styles is the painful concession (HTMX-driven
inline styles + our existing inline-SVG icons). The other
directives are tight. Migrating to nonce-based CSP for styles
later is a follow-up.

**Implementation:**
- New `CSPMiddleware` in `src/wobblebot/web/middleware.py`
  alongside the existing CSRF middleware
- Policy is built from a config dict so operators can tune per
  deployment (some browsers + extensions need exceptions)
- Add to `app.add_middleware` chain in `web/app.py`
- Tests verify the header appears on `/dashboard` + does NOT
  appear on `/static/*` responses (those should keep
  Cache-Control + Content-Type only)

**Trigger:** v1.1 hardening pass. Same trigger as MFA — any of
"shared, multi-user, internet-exposed" pushes this from
nice-to-have to required.

### Multi-operator web auth

**What:** ADR-017's single-operator boundary lifted. Multiple user
rows, role-based access (e.g. "read-only" vs "operator"), maybe SSO.

**Why deferred:** v1.0 is single-operator. The web UI is LAN-only
by default; attack surface is operator-controlled hosts.

**Trigger:** the project gains contributors who need read-only
production access.

### Richer charts in web UI (price history, PnL curves)

**What:** server-rendered SVG via a small charting library
(matplotlib + plotly are heavyweight; lightweight alternatives
exist). Stage 7 shipped tables; v1.1+ could ship plots.

**Why deferred:** Stage 7 prioritized data accuracy + ADR-013
firewall integration over visualization. ADR-016's "no SPA"
commitment still applies — charts must be server-rendered.

**Trigger:** operator points at the web UI and says "I can't see
the trend here, I have to mentally aggregate the table rows."
