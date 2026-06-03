# Operator UX — Discord, web UI, CLI ergonomics

*Operator-facing improvements across all surfaces. The web UI is the largest cluster (Stage 7 substrate), with Discord, `cli/recalibrate`, daemon orchestration, and hosting topology rounding out the list.*

*Companion to [`v1.0-future-improvements.md`](../v1.0-future-improvements.md) (the catalog index) and [`v1.0-known-limitations.md`](../v1.0-known-limitations.md) (what v1.0 explicitly does NOT do).*

### Web UI first-run admin user wizard

**What:** when `operator.db` has zero rows in the `users` table,
the login page redirects to a `/setup` route that shows a
"create first admin" form (username + password + confirm).
After successful submission, a bcrypt-hashed row is written and
the `/setup` route is permanently guarded (any subsequent GET
redirects to `/auth/login`).

**Why:** today the operator must SSH to the host and run
`docker exec -it wobblebot-web python -m wobblebot.cli.web
create-user` interactively. Easy to forget (just happened
2026-05-27 during NAS Docker deploy: stack came up, dashboard
load showed login page, no user existed). For friend-deployment
this is a hard blocker — friends don't have SSH access.

**Alternatives considered + rejected:**
- **Auto-create with random password logged once.** Random
  password landing in container logs is a credential-leak
  surface; depends on operator catching it before log rotation
  / aggregation. Standard practice has moved away from this.
- **Env-var driven** (e.g. `WOBBLEBOT_ADMIN_USERNAME` +
  `WOBBLEBOT_ADMIN_PASSWORD_HASH` in Portainer stack env).
  Acceptable but adds a second credential surface to manage
  alongside `WOBBLEBOT_WEB_SESSION_SECRET`. Plaintext password
  variant is an outright leak surface.

**Implementation sketch:**
- New `/setup` route in `wobblebot.web.app` (no auth required;
  serves the create-form GET and processes the POST).
- New `wobblebot.web.middleware` gate: if `users` count == 0,
  any request that's not to `/setup` or `/static/*` 302s to
  `/setup`. After count >= 1, `/setup` itself 302s to
  `/auth/login`.
- Re-use `wobblebot.cli.web.create-user`'s bcrypt logic in the
  POST handler so there's one place that computes the hash.
- Tests: existing `tests/web/test_auth_routes.py` pattern;
  new `tests/web/test_setup_route.py` for the guard semantics.

**Why deferred:** v1.0 ships with operator-only deployment;
single SSH'd `create-user` invocation per fresh install is the
acceptable manual step. Becomes pressing when friend-deployment
lands and friends need a no-SSH onboarding path.

**Trigger:** friend-deployment (Tier 1 onwards in the entry
below). Pair the two.

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

### Discord confirmation UX: replace emoji reactions with UI buttons

**What:** swap the current confirmation embed's pre-populated
✅ + ❌ reactions for `discord.ui.View` + `Button` components.
The operator clicks an actual button labelled "Approve" or
"Reject" instead of clicking one of two emoji reactions the bot
already placed.

**Why it bothers operators:** when the bot pre-populates ✅ + ❌
on its own confirmation embed (Stage 5.2's ``send_confirmation``),
the visual result is "WobbleBot reacted with ✅ 1 and ❌ 1 to its
own message." Reading the embed's footer ("react ✅ to approve,
❌ to reject") clarifies that they're vote buttons, not the bot
endorsing both outcomes — but several seconds of "wait, did the
bot just approve AND reject?" confusion is the experience until
the operator parses the footer. The 2026-05-24 soak surfaced
this as visually weird-but-not-broken.

**Implementation:** v1.0 uses emoji reactions because Stage 5.2's
implementation predated discord.py's `View`/`Button`/`Interaction`
machinery being commonly idiomatic. v1.1 candidate:

1. Replace ``DiscordTransport.send_confirmation``'s embed-plus-
   reactions with an embed-plus-View. The View carries two
   `discord.ui.Button` instances (Approve / Reject) with
   `style=ButtonStyle.success` / `ButtonStyle.danger`.
2. Each button's callback transitions the matching
   `PendingCommand` row to `approved` / `rejected` directly —
   replaces the current ``_handle_reaction`` + in-memory
   `pending_message_map` indirection.
3. Per ADR-013 decision 3 the firewall stays intact: the button
   click writes to ``pending_commands`` with `status='approved'`
   and cli/live's ADR-002 poll picks it up. No engine call
   bypass.
4. Buttons auto-disable after click (`view.stop()`), so the
   operator can't double-approve a single PendingCommand.

**Why deferred:** the emoji-reaction flow works end-to-end. The
``_handle_reaction`` handler + `pending_message_map` are
exercised by 5+ existing tests; swapping them for Views means
rewriting those tests against the discord.py interaction-event
shape. Worth doing once for cleaner UX, but not v1.0-blocking.

**Trigger:** post-v1.0 tag. Pair with the Stage 5.4 reaction
handler simplification — `pending_message_map` (in-memory) goes
away once the button callback has the `pending_id` baked in
via View construction.

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

**Banner content — surface the decision economics, incl. projected loss
(operator note 2026-05-29).** Beyond the misalignment facts, the banner
should show the *projected loss of re-anchoring* so the operator weighs
acting vs. waiting. This is the human-in-the-loop instrument for the
"re-anchor stays operator-confirmed" posture (the fee-asymmetry concern
from the 2026-05-29 strategy discussion: a re-anchor realizes a cost
*now*; waiting may recover it). Target banner format:

> ⚠ Consider re-anchoring BTC/USD. Current price $73523.70 is 2.7
> spacings from the nearest open order; grid anchored at $74769.80,
> oldest order 1h 29m old. Re-anchoring would reposition the ladder to
> catch the current band.

…**plus a projected-loss line.** Open design question — what "projected
loss" should mean: the realized/paper loss on the inventory the
re-anchor would strand (BTC bought near the old anchor whose counter-SELLs
sit above market and would be abandoned), **plus** the fee cost to cancel
+ rebuild the ladder, weighed against the opportunity cost of leaving
capital parked offside. Computable from open orders + fill history +
current price. Decide whether to show realized-if-sold, paper-loss-on-
stranded-inventory, fee-only, or all three. This is the surface that
makes "operator confirms re-anchor" an *informed* decision rather than a
blind button-press.

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

### "Today's PnL" — split cycle realization-day from earning-day

**What:** v1.0's "Today's PnL" headline (dashboard + Discord
`status_report` + operator `/dashboard`) sums `cycle.net_pnl`
across cycles whose SELL fired today in the operator's timezone
(`services/cycle_matcher.py:177-220` `today_realized_pnl`). The
matcher pairs BUYs to SELLs via amount-equality primary
(`cycle_matcher.py:131-137`) and falls back to "oldest cheaper"
(`cycle_matcher.py:139-141`) for pre-engine, manual, or
re-anchored fills where the engine's actual counter-pair was
canceled. When the fallback fires, a SELL closing inventory
from days ago can produce a single anomalously-large cycle whose
PnL is mostly multi-day price drift, not today's grid spread.
The headline then misrepresents what the bot earned today.

**Concrete example (2026-05-26 soak):** dashboard reported
**+$0.3460** for the day. Trade history showed one SELL today
(0.00012879 BTC @ $77,643.30) matched via fallback to a BUY from
2026-05-23 (0.00013410 BTC @ $74,568.30). Spread $3,075/BTC ×
0.00012879 − $0.05 fees = $0.3460 — arithmetically correct, but
the May 23 BUY's actual engine-placed counter-SELL never fired
(cap-trip + re-anchor on 2026-05-22), so the operator effectively
held inventory for 3 days through upward drift. The other 4
matched cycles in the trade history all sit at $0.0508–$0.0518
(normal 1% grid spread). The $0.3460 was the only outlier and
it was the fallback cycle. Cash-basis verification: cost basis
on the matched 0.00012879 BTC slice = $9.5995 + $0.025 fee =
$9.6245; SELL proceeds = $9.9726; difference = $0.3481 ≈ matcher's
$0.3460.

**Two implementation paths:**

1. **Split the headline.** Render `Today's PnL: $0.3460 (normal
   $0.00 + recovery $0.3460)` where "normal" sums cycles whose
   BUY and SELL both fired within a normal-grid window (e.g.
   24h) and "recovery" sums fallback cycles whose BUY is older.
   Operator sees the realized cash flow AND the breakdown.
2. **Annotate per-cycle in the Recent Cycles list.** Flag cycles
   where `sell.executed_at − buy.executed_at > N hours` with a
   small icon or "long hold" label. Headline stays as-is; the
   operator drills in for context.

Recommended starting point: option 2 (cheaper change; doesn't
relitigate the headline semantics; surfaces the same information
without re-bucketing). Promote to option 1 if the headline keeps
producing confusion across soak cycles.

**Implementation:** the matcher already knows which pairing
heuristic fired (the loop at `cycle_matcher.py:131-148` is the
discriminator). Extend `RecentCycle` with a `pairing_method`
enum (`engine_counter` | `fallback`) and a derived
`hold_duration` property. Template logic in
`_status_card.html` / Discord embed renderer reads those fields;
no engine code changes. `today_realized_pnl` either gains an
optional `pairing_filter` parameter (option 1) or stays
unchanged (option 2).

**Why deferred:** v1.0 ships the matched-cycle math correctly —
the 2026-05-23 fix (`engine.md` "ledger reconciliation
companion fix") swapped the matcher's primary heuristic from
FIFO-cheapest to amount-equality, which is exactly right for
the engine-counter case. The fallback path is the residual
ambiguity and only fires when the trade log has unmatchable
inventory (re-anchor / manual fills / pre-engine seed). The
display question is a presentation choice that benefits from
operator preference data — easier to gather post-tag.

**Pairs with:** the `cycle_matcher.py` docstring at lines 31-39
already names the re-anchor + manual-fill cases as known
heuristic limitations; this entry adds the display-layer
mitigation.

**Trigger:** operator-flagged 2026-05-26 after the cap-trip
recovery cycle ($0.3460 fallback) produced a headline that
diverged from the day's actual grid earnings ($0.00 in normal
cycles). Repeated occurrences during multi-coin soak (every
re-anchor or manual close creates one) would promote this from
candidate to scheduled.

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

**Status (2026-06-03 — largely SHIPPED):** the live multi-coin
dashboard renders per-symbol subsections with the current price
baked into each subhead (option 2), as the soak's 6-coin run shows
— the old "soak runs 1 coin (BTC/USD)" rationale is stale.
Remaining: (1) the **parked / no-order symbol** sub-gap — a
configured symbol with no open orders renders a bare header with no
price (dedicated entry below); (2) option 1's CSS-grid wrap is only
worth it if per-symbol subsections get unwieldy past ~8 coins — not
a concern at 6.

**Trigger:** per-symbol grouping already triggered (multi-coin
soak). The parked-symbol price sub-gap is operator-flagged
2026-06-03; the width/wrap refinement waits for ~8+ coins.

### Symbol-card price + indicator for parked / no-order symbols

**What:** the dashboard's per-symbol cards bake the current price +
up/down indicator into the header (`DOGE/USD $0.09 ▼`) for symbols
that have open orders, but a *configured* symbol with no open
orders — parked because it's offside per ADR-006, or simply held
and inactive — renders a bare `BTC/USD` with no price. Render the
price + indicator in the header for EVERY configured symbol
regardless of open-order state; a held, tradeable asset should read
like the others.

**Why:** surfaced 2026-06-03 — BTC was in the live grid but offside
& parked the whole session, so its card showed no price while
ETH/SOL/XRP/DOGE/ADA all did. The operator wants the price visible
for any coin in the account, trading or not.

**How:** the per-symbol header already renders price from
`StatusSnapshot.current_prices` for order-bearing symbols. First
confirm the snapshot actually carries a parked symbol's price —
`current_prices` is built from the price poll, which *should* cover
every configured symbol, but verify a parked/offside symbol isn't
skipped. If present, it's a template conditional (render the header
price block even when the symbol's open-orders list is empty). If
absent, also plumb the price for no-order symbols. Pairs with the
per-symbol held-inventory card (Buying-power item) — same "show the
held/tradeable asset even when idle" theme.

**Why deferred:** dashboard polish, not gating. Branch-safe,
template-ish (a one-line conditional if the data's already present).

**Trigger:** operator-flagged 2026-06-03 from the multi-coin soak
dashboard — BTC parked, no price shown, while the trading alts all
displayed price + indicator.

### Whole-UI design review — punch list (2026-06-03)

A frontend-design pass over every page (auth, dashboard, health, cost,
advisor, harvester, news, history, notifications, settings, command
flow) + `base.css`. The app is well-engineered + consistent with strong
empty-state discipline; these are the improvement candidates. Items
already slated elsewhere (session card, buying-power/held-inventory,
lifetime PnL, per-order delta, recent-fills enhancement, parked-symbol
price, mode-badge) are NOT repeated here.

**Defects (cheap, fix-worthy on their own):**
- **Notification level-color inconsistency.** `info` renders blue
  (`info`) on `notifications.html:39` but green (`ok`) on
  `history.html:82` — same notification, different color across pages.
  Pick one (info ≠ success → use `info`/blue). ~2-line fix. **✅ FIXED
  2026-06-03 — `history.html` info → `info`/blue, matching
  `notifications.html`.**
- **Zero responsive CSS.** `base.css` has no `@media` rule except
  `prefers-color-scheme` — the 6-7-column tables (Recent Fills,
  harvester) overflow on mobile and the navbar crowds. If the operator
  ever checks on a phone, it's broken. Min: a horizontal-scroll wrapper
  on wide tables + a nav collapse below ~700px.
- **Settings inputs dark-on-light** + **`.muted` declared twice** — both
  flagged in-code "for the dark-mode cleanup" that never landed. **✅ FIXED
  2026-06-03:** settings `.form-row` inputs now theme via `--surface-card`/
  `--text-primary`/`--border-strong` (the dead `--form-input-*` vars removed);
  the duplicate `.muted` collapsed to one rule.

**Tier 1 — high value, low effort:**
- **Dashboard scoreboard strip.** **✅ DONE 2026-06-03 (v1.1).** A
  top-of-status-card strip now leads with the answers — Account value ·
  Free USD · In positions · Today's PnL · Lifetime PnL — reusing the
  `.metric` hero `/cost` already uses. Money cells come from `observe.db`
  balance snapshots (credential-free, "as of HH:MM" stamp) and degrade to
  "—" when unwired; the buried 13px Today's-PnL span was removed. This
  folded in the buying-power *aggregate* + the lifetime-PnL items; the
  per-symbol held inventory *inside each card* remains.
- **`/cost` spend-by-day bar chart.** "Spend by Day (Last 7 Days)"
  (`cost.html:20`) is a 7-row table begging for a 7-bar inline-SVG
  chart.

**Tier 2 — high value, medium effort:**
- **Per-symbol grid-band sparklines.** A tiny inline-SVG sparkline
  (recent price + grid levels + current marker) per symbol card makes
  "offside/parked" *visible* instead of inferred. Highest "feels like a
  trading instrument" lever; no chart lib.
- **Fill-flash micro-interaction.** Flash a just-filled row on the 15s
  HTMX swap. (`/cost` also lacks the dashboard's `transition:true`.)
- **Fill toast (Kraken-style)** *(operator idea 2026-06-03)*. A
  bottom-right popup when an order fills, auto-dismissing after a few
  seconds and progressing through each fill that landed since the page
  was last active (modeled on Kraken Pro). Richer than the fill-flash
  row-highlight above — a transient queue of "X filled @ $Y" cards.
  Source data: the same `recent_trades` the status card already polls;
  the client diffs against the last-seen trade id (mirrors the
  bell-badge last-seen pattern in `layout.html`). HTMX/JS, no new
  backend. Pairs with the per-symbol sparkline + scoreboard for a
  "live instrument" feel.
- **Advisor pagination/collapse** (`advisor.html:34`) — every
  suggestion is a full stacked card with a nested table; collapse older
  ones once `cli/advise` accumulates volume.

**Tier 3 — optional aesthetic elevation:**
- **Typography + brand.** System stack everywhere (`base.css:205`);
  `--color-link` is Facebook-blue. Put numeric columns in a tabular
  monospace (reads like a terminal) + carry the login page's teal
  (`#4dd0e1`) past auth. The "stop looking like a generic admin panel"
  lever.

**Small:**
- News `mentioned_coins` as `.tag` pills, not a comma `<code>` list
  (`news.html:80`), for vocabulary consistency; render the collected-
  but-hidden `publisher` field.
- Emergency-stop confirm page (`command_confirm.html`) gets the same
  generic treatment as a routine pause — give the highest-consequence
  action stronger visual weight on its confirm screen.

**Trigger:** operator-requested whole-UI review 2026-06-03. All P3 /
post-tag; the defects are cheap enough to pull forward if they bite.

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

### Discord status_report tally section: compact table instead of stacked fields

**What:** the status_report embed renders its 8+ tallies (Balance,
Today's PnL, Open orders, Fills, News, Suggestions, Harvester
band, Proposals) as separate embed fields with ``inline=False``,
which Discord stacks vertically -- label on one line, value on
the next. The result looks like:

```
Balance
$79.92
Today's PnL
$0.0000
Open orders
5
Fills (last 1h)
0
News (last 1h)
6
...
```

That's ~16 vertical lines after the narrative, which dominates
the embed on mobile and forces the operator to scroll past the
narrative they actually wanted.

**Fix options (pick at implementation time):**

1. **inline=True on tally fields.** Discord packs inline fields
   three-per-row, so eight tallies collapse to 2-3 lines. Smallest
   change -- only requires plumbing per-field ``inline`` through
   ``DiscordTransport.send_embed`` (currently hardcodes False) and
   the renderer's tally output.
2. **Single description-style table.** Combine all tallies into
   one Markdown table in a single ``inline=False`` field. Better
   visual alignment than option 1's auto-pack, but Discord's
   monospace rendering in embed fields is inconsistent across
   client versions.
3. **Move tallies into the embed description** (above or below
   the narrative). Frees the fields surface entirely. Tradeoff:
   harder to scan because each tally is plain text rather than
   labelled.

Recommended starting point: option 1. Minimal blast radius;
visually compact; trivially reversible if it surprises anyone.

**Why deferred:** v1.0's stacked layout is verbose-but-correct.
Cosmetic UX issue, not a data correctness one.

**Trigger:** operator-flagged 2026-05-24 after the post-restart
probe battery showed how dominant the tally stack is relative
to the narrative.

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

### `weather_report` query — market-trend summary across multiple days

**What:** a sibling query to v1.0's `status_report` (which condenses
the operator's bot-internal activity since the last brief). The
`weather_report` (or whatever final name lands — `market_brief`,
`forecast`, `daily_outlook` are alternates) would aggregate
EXTERNAL market signals rather than the bot's own activity:

- News headlines + sentiment scores over the past N days (default
  3-7 — wider window than `status_report`'s "since last")
- Price trends per monitored symbol (e.g. ETH 7d % change, BTC
  realized volatility)
- Volume + spread anomalies (e.g. unusual taker fee activity)
- Recent advisor suggestions across all symbols (the LLM's own
  trend assessments)
- Possibly: integration with a generic external sentiment feed
  (CryptoCompare social score, Fear & Greed Index)

Then condense via the same `AssistantPort.summarize` pattern
`status_report` uses, with a prompt tuned for forward-looking
commentary: "Based on the news + price + sentiment data below,
what's the market mood for the next 24-72 hours? Flag anything
that should make the operator adjust grid spacing or pause a
symbol."

**Why deferred:** the underlying pieces all exist (news.db,
observe.db price history, advise.db suggestions) so the
groundwork is in place. Defer because:

1. Operator wants `status_report` first to internalize the
   pattern + tune the prompt before adding a second flavor.
2. Naming + scope are still soft — could overlap with the v1.1
   "advisor across multiple symbols" candidate or get folded
   into a richer `cost_summary` if those land first.
3. Requires a price-trend computation layer that doesn't exist
   yet (current `/observe` ingests but doesn't compute deltas).

**Trigger:** after `status_report` is in regular operator use
and the prompt has stabilized; OR when a clear external-sentiment
signal lands (e.g. CryptoCompare 90-day evaluation per ADR-010
unlocks richer sentiment data).

**Naming note:** "weather report" is operator-catchy; the formal
catalog name should still be `<noun>_report` for symmetry with
`status_report`. Candidates: `market_report`, `weather_report`,
`outlook`, `forecast`. Decide at implementation time.

### `AssistantPort.summarize` — cloud provider implementations

**What:** v1.0's `status_report` query uses a new
`AssistantPort.summarize(system_prompt, user_content, max_tokens)`
method to generate prose narratives. Only the Ollama adapter
implements it today; Anthropic / OpenAI / Google adapters raise
`NotImplementedError` with a "switch to ollama" message. If the
operator switches `operator.assistant.provider` to a cloud
provider, `status_report` (and any future `*_report` queries
that ride on `summarize`) stops working.

**Implementation:** mirror each adapter's `parse_intent` shape
but skip the JSON schema validation. Each adapter:

- Builds a chat-completion request with the system_prompt as the
  system message and user_content as a single user message.
- Routes through the existing `execute_assistant_call` cost gate
  (Phase 6 / ADR-014 — cost-cap enforcement is non-negotiable
  for cloud providers).
- Wraps response parsing to return the raw assistant message
  text (using each provider's existing `parse_text_fn` helper).
- Honors the `max_tokens` parameter — important because
  `status_report` uses 2048 tokens, much more than `parse_intent`'s
  typical 512.

**Why deferred:** operator runs Ollama; cloud paths are
configured but not actively used. Implementing across three
providers adds ~200 LOC + provider-specific cost-gate tests
that v1.0's status_report doesn't need.

**Trigger:** if the operator switches to cloud, OR if a future
v1.1 feature (weather_report, anomaly_summary) needs the
broader provider coverage.

### Mode-parameterized webui — reuse the dashboard for live + shadow

**What:** serve the **same** web UI for both trading modes instead of
building a separate shadow dashboard. The `LIVE`/`SHADOW` `mode-badge`
becomes **dynamic** (rendered from the active mode, not hardcoded `LIVE`
at `_status_card.html:35`); the mode parameter switches *how the app
responds* — chiefly which data source it reads (the live ledger vs
`cli/shadow`'s synthetic ledger) — while every template, route, and
style stays identical. The CSS already ships both badge variants
(`mode-badge-live` / `mode-badge-shadow`, `base.css:1449`), so the
presentation side is a context-variable flip.

**✅ Badge + single-source DONE 2026-06-03 (v1.1).** The badge now reads
`application.mode` (`live | shadow | sandbox`) — the **single**
deployment-mode config — via the `trading_mode` Jinja global; `cli/web`
passes `config.application.mode` to `create_app`. `application.mode` was
promoted from informational-only YAML to a modeled `ApplicationConfig`
field; the redundant `web.mode` knob was removed (operator: one mode
source, not two). All three badge variants ship in CSS. **Remaining:**
the *data-source* switch (point the loaders at the shadow ledger) — see
the Mode-source note below.

**Why (operator decision 2026-06-03):** don't reinvent the wheel for a
shadow UI. The dashboard is already mode-agnostic except for that one
hardcoded badge; DRY says reuse it. Mode is a runtime concern, not a
template fork — it pairs with `cli/up shadow` (below), which already
distinguishes the two daemon sets.

**How / design questions to settle when built:**
- **Mode source:** likely a separate `cli/web` instance per mode
  (pointed at the shadow DBs via config) — simplest, mirrors `cli/up
  live` vs `cli/up shadow` running their own stacks. Alternative: one
  instance that resolves mode + data source from config/env.
- **Data source:** `web.live_db` → the shadow ledger in shadow mode;
  the snapshot loaders are already DB-path-parameterized.
- **Mutations/firewall:** the `pending_commands` ADR-013 firewall still
  applies in shadow mode — commands target the shadow engine. Confirm
  the confirm-flow copy reads correctly when the target is paper.
- The **badge-dynamic flip is the small, branch-safe first slice**; the
  data-source + mode-selection plumbing is the larger piece.

**Supersedes:** the code comments that assumed a *separate* shadow page
(`_status_card.html:31`, `base.css:1429` say "future shadow-dashboard
page/variant") — update those when this lands.

**Why deferred:** not needed until the operator runs `cli/shadow` as a
standing paper-trading instance. Branch-safe to start (the badge flip).

**Trigger:** operator wants to watch a shadow run in the browser, OR the
`cli/up shadow` orchestrator lands and a paper stack wants a UI.

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

### Friend-deployment onboarding — guided setup (dummy-proof scope)

**What:** lower the bar for a non-WobbleBot-developer to set up
their own self-hosted instance with their own Kraken keys (NOT a
hosted-as-service path — see the standing rule on declining
Kraken upsell + hosted-SaaS nudges). The friend-deployment
scenario surfaced 2026-05-26 (soak Day 9) when the operator's
colleague expressed interest in trying WobbleBot.

The original framing of this entry (`cli/init setup wizard`)
underestimated the scope. The friend doesn't just need WobbleBot
configured — they need a **prerequisite stack** that the operator
already has on their machine and forgets exists:

1. **Python 3.13+** with pip + venv
2. **LLM backend** — either:
   - **Local Ollama**: install Ollama, pull a recommended model
     (3-10GB), have ≥8GB RAM (16GB recommended), ideally a GPU
     for tolerable latency. See `docs/reference/operator-llm-models.md`
     for the recommended picks per hardware tier.
   - **Cloud LLM**: account with Anthropic / OpenAI / Google,
     billing set up, API key minted, cost-cap configured per
     `services/llm_cost_gate.py` semantics.
3. **Kraken account** with three keys per ADR-003 separation
   (read-only / trade / harvester), each with the right scope
   checkboxes set + optional IP restriction
4. **Discord bot** (optional but operator-default): bot token,
   server invitation, channel + user allowlists
5. **WobbleBot itself**: clone, install, `.env`, `settings.yml`,
   web session secret, run `cli/preflight`, run the daemons
6. **Docker** (optional, deployment-topology-dependent): WobbleBot
   v1.0 ships as bare Python daemons; the `docker/` directory is
   a Phase-2-placeholder that never landed. A friend deploying
   via Synology Container Manager / Portainer / Docker Desktop
   needs container orchestration the project doesn't ship yet.

**"Dummy proof" is the real ask** — and dummy-proofing #1-#6
above is several discrete projects:

- A `cli/init` wizard handles #3-#5 cleanly (interactive prompts,
  Kraken read-only verification, `.env` writes, preflight
  invocation). Pure Python, no new runtime deps. **Realistic v1.1
  candidate.**
- A self-contained executable (PyInstaller / Nuitka) helps with
  #1 only. Doesn't help with #2 (Ollama install / cloud-LLM
  signup), #3 (Kraken key minting on Kraken's UI), #4 (Discord
  bot creation on discord.com), or #6 (Docker setup). The binary
  shaves "install Python + pip install" off the prereq stack but
  leaves the four other prereqs un-shaved.
- Ollama + cloud LLM + Docker prereqs are inherently outside
  WobbleBot's reach. The wizard can DETECT their presence and
  guide the friend to install/configure them, but cannot install
  them ITSELF without elevating to a platform-specific installer
  (MSI on Windows, .pkg on macOS, .deb/.rpm on Linux). That's an
  order of magnitude more packaging work than a Python wizard.

**Realistic scope tiers (pick where to stop):**

| Tier | What it covers | Effort | Trigger to pick up |
|---|---|---|---|
| **0. Friend-instance runbook** | Markdown checklist the operator hands to the friend (or walks through together). No code change. Discord, Ollama, Docker setup all documented manually. | 1-2 hours | Available NOW for the colleague's soak-end deployment |
| **1. `cli/init` wizard** | #3 + #4 + #5 (WobbleBot config, Kraken key wiring, Discord-bot guided web flow, preflight). Prereqs #1, #2, #6 still manual but documented. | 2-4 days | Friend-instance runbook validated against ≥1 real deployment; friction points catalogued |
| **2. Wizard + Ollama detection/install guidance** | Tier 1 + interactive "do you have Ollama? here's the platform-specific install command" branch. Wizard SHELLS OUT to Ollama CLI but doesn't bundle Ollama yet. | +1-2 days on top of Tier 1 | Operator decides Ollama-on-friend-machine is the typical path (vs cloud-only) |
| **3. Cloud-LLM signup walkthrough** | Tier 1 + cloud-only branch that prompts for provider choice, API key, and writes the cost-cap defaults. Cheaper for friends without GPU hardware. | +1 day on top of Tier 1 | Operator decides cloud-LLM is the default friend path (likely true — most friends don't have $1500+ GPUs) |
| **4. Self-contained binary + Ollama bundled** | Tier 1-3 packaged via PyInstaller per platform, **with Ollama redistributable bundled alongside** (Ollama is MIT-licensed; ~500MB-1GB added to installer size). Friend downloads ONE file, runs it, gets WobbleBot + Ollama runtime extracted. Models still pulled at first run by Ollama (separate ~3-10GB download). Kraken keys + Discord bot still external. | +3-5 days plus CI build infrastructure + Ollama version-pin maintenance | Tier 1-3 has been used by ≥2 real friend deployments and operator decides "the runtime install steps are the actual bar to entry" |
| **5. Platform installer (MSI / .pkg / .deb) — only AFTER WobbleBot ships Docker support** | Tier 4 + OS-level installer that ALSO offers Docker Desktop install (detect → open download URL; **NOT bundle** — Docker Desktop is closed-source with periodically-changing commercial license terms). Linux variant uses distro Docker Engine packages. **Prerequisite:** WobbleBot must ship its own Dockerfile + compose first (currently a Phase-2 placeholder in `docker/` with no content). | Weeks. New maintenance class (per-OS installer testing matrix + Docker Desktop license tracking). | Almost never for solo project. WobbleBot's own Docker support landing first is itself a v1.1 candidate (see `engine.md`-adjacent or this entry's sequel). |

**Per-tool bundling reality (clarified 2026-05-26):**

- **Ollama: bundleable.** MIT-licensed, redistributable, ~500MB-1GB
  runtime. Models still pull at first run. Lands at Tier 4.
- **Discord: NEVER bundleable.** No installer to ship — the friend
  has to log into discord.com/developers/applications in their
  browser, create an app, mint a bot token, configure OAuth2 invite
  URL. The wizard can open the right URL with side-panel
  instructions but can't automate the manual web flow. Every tier
  treats Discord as guided-but-manual.
- **Docker: premature and complicated.** WobbleBot v1.0 doesn't
  ship Dockerfile + compose — the `docker/` directory is an empty
  Phase-2 placeholder. Bundling Docker before WobbleBot uses
  Docker is solving a problem that doesn't exist yet. When it
  becomes relevant: Docker Desktop's commercial license terms have
  shifted twice since 2020, making bundle compliance fragile.
  Detect-and-link is the practical pattern, not bundle.

**Recommended path:** ship Tier 0 first (now-ish, post-tag), let
the friend's actual deployment validate which prereqs were the
real friction points, then decide Tier 1-4 based on data. **DO
NOT design for Tier 5 ambitions on Tier 0 data.**

**Important framing for the operator:** the friend handing off
their Kraken key to a binary you built (Tier 4+) creates a
**trust event** that didn't exist when both of you ran the same
source they could read. The bundled binary should ship with the
source hash + a "you can also install from source if you don't
want to run a binary I built" link in the wizard's intro screen.
Standard crypto-tool hygiene; matters in this domain because the
binary gets read+write access to their trading account.

**Why deferred:** Tier 0 is reasonable to ship post-v1.0 tag.
Tiers 1-4 wait for soak-end + the colleague's actual deployment
experience. Tier 5 isn't on the roadmap.

**Pairs with:**

- The companion `docs/deploy/friend-instance.md` runbook (Tier 0
  above — operator-facing checklist for "I'm helping a friend
  set this up on their machine"). Strongest precondition for
  any wizard work.
- The "Multi-operator web auth" entry (also in this file) — if
  a friend's deployment ever grows into a second person on the
  same hardware, the single-operator boundary in ADR-017 lifts
  at that layer.
- The standing rule against hosted-SaaS-with-fee-model — the
  wizard + runbook is the self-host alternative that satisfies
  the "I want to share WobbleBot" impulse without inheriting
  the SaaS regulatory load.
- `docs/reference/operator-llm-models.md` — per-hardware-tier
  model recommendations. The wizard's Ollama branch consults
  this for its model-pick prompt.

**Trigger:** operator's colleague's interest moves from "would
try it" to "let's set up a date" — at which point Tier 0 is
the right answer. Anything beyond Tier 0 needs the data Tier 0
generates first.

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

### Math-specialist LLM integration paths

**What:** wobblebot is fundamentally a numerical-reasoning
application -- prices, percentages, ratios, fee accounting,
volatility, position sizing. The current LLM choices (phi4,
mistral-nemo) are general instruct models that handle math
OK but aren't optimized for it. Math-specialist models
(phi4-mini-reasoning, mathstral, wizardmath) are blocked
from the operator-assistant role today because they can't
produce schema-conforming intent JSON, but they may produce
better-quality output in any role where the LLM's job is to
crunch numbers and explain results in prose.

**Candidate roles (none wired today):**

1. **MoE quant-expert** -- Phase 3.4's `quant.md` advisor
   slot is the obvious first target. Already designed to do
   numerical analysis of performance summaries; a math
   specialist is the on-paper-correct fit. Implementation:
   add a model-suitability check to the AdvisorPort path
   that mirrors the OllamaAssistantAdapter's, but evaluates
   against AdvisorRecommendation schema instead of
   OperatorIntent.
2. **Backtest / cycle analysis prose** -- new feature.
   Operator asks "explain my last 50 cycles", math specialist
   produces a Sharpe / Sortino / drawdown / win-loss prose
   summary. Mirrors the `status_report` summarize pattern
   but with a math-specialist prompt + AssistantPort.summarize.
3. **Anomaly detector explanation layer** -- pairs with the
   v1.1 anomaly detector daemon (deterministic Z-score). Math
   specialist explains WHY a value is anomalous in operator-
   friendly prose.
4. **`weather_report` query enrichment** -- the v1.1
   weather_report entry already calls for market-trend math
   (sentiment + price + volume aggregation). Math specialist
   is a natural fit for the aggregation step.
5. **Cost-honesty dashboard math** -- the v1.1 cost-honesty
   entry calls for infrastructure-cost / electricity / fees
   per cycle accounting. Heavy arithmetic plus operator-
   friendly explanation.
6. **Recalibration recommendations** -- extension of
   `cli/recalibrate`. The current scaler does linear math;
   a math specialist could reason about non-linear effects
   (Kraken's tiered fee schedule, slippage characteristics
   at different order sizes, optimal grid density for a
   given realized volatility regime).

**Implementation pattern:**

Each candidate role would need:
- A dedicated prompt file in `config/prompts/<role>.md`
- Either a new adapter (if the schema differs from existing
  advisor / assistant) or reuse of `AssistantPort.summarize`
  for the prose-only cases
- Configuration in `settings.yml` to pick the math-specialist
  model for that specific role (separate from the operator-
  assistant model)
- A model-suitability list scoped to that role (mirror the
  `KNOWN_INCOMPATIBLE_FOR_ASSISTANT` pattern -- "models that
  can't do math well" would be blocked from quant roles)

**Why deferred:** v1.0 trades successfully with the existing
LLM substrate; the math-specialist paths are quality
enhancements, not gaps. The work is best done one role at a
time as features land (MoE quant when MoE goes live, weather
report when that feature ships, etc.) rather than as a
speculative scaffold.

**Trigger:** any of the six candidate roles getting prioritized
for v1.1+ work. Operator-flagged 2026-05-24 ("mathematical
specialists I think have a very special role in our app, and
we should try to include them if possible"). Cross-reference:
the math-specialist rejection scope note in
`docs/reference/operator-llm-models.md` enumerates the same
candidate-role list with implementation specifics per role.

### Reasoning-model support — DROPPED 2026-05-26 after v2 follow-up

**Status:** ❌ Dropped from active v1.1 work after two
investigation cycles failed to deliver differentiated value.
Compact-prompts approach is a dead end at small reasoning-model
scale; large reasoning models work but don't justify their
latency over non-reasoning peers.

**Original ambition:** add first-class support for reasoning-tuned
models in the operator + advisor roles via (1) compact prompt
variants for small (3.8B) reasoning models and (2) per-role
`force_json_output` opt-in for chain-of-thought suppression.

**Two investigation cycles closed it:**

- **2026-05-25 first-pass.** Compact prompts shipped + initial
  sweep across `phi4-mini-reasoning:3.8b-fp16`,
  `phi4-reasoning:14b-plus-q8_0`, `deepseek-r1:14b-qwen-distill-q8_0`.
  Result: compact prompts produced regressions on both roles
  (quant-compact 8/18 over-widen; operator-compact 0/29 invent-keys);
  large models worked under `format=json` but landed at the 11/18
  lazy-baseline cluster ("always slight widen") with no
  differentiation from non-reasoning peers.
- **2026-05-26 v2 follow-up.** Redesigned both compact prompts
  preserving the constraints v1 dropped (magnitude anchor in
  quant; "router not answerer" framing + anti-pattern examples
  in operator). Sweep against `phi4-mini-reasoning:3.8b-fp16`:
  - operator-compact v2: **0/29 (29 errors)** — identical
    failure mode to v1; model still hallucinates off-topic
    content (database joins, geometry queens, pollution
    levels). The added framing didn't reach the model.
  - advisor-compact v2: **4/18 (4 errors)** — regression from
    v1's 8/18. The ±25% magnitude rule worked when the model
    produced valid JSON (spacing=1.05 within band), but the
    longer prompt re-triggered the saturation that v1's
    smaller size avoided.

**Verdict:** at 3.8B params + reasoning fine-tuning,
phi4-mini-reasoning can EITHER produce valid JSON under a short
prompt OR honor magnitude constraints under a longer prompt, but
not both. The original `KNOWN_INCOMPATIBLE_FOR_ASSISTANT` block
holds. The 14B+ reasoning models (`phi4-reasoning:14b-plus`,
`deepseek-r1`) work but score at the lazy baseline (11/18 advisor)
or carry significant latency penalty (deepseek-r1 operator at
25s under force_json vs ~13-14s for non-reasoning models).

**What we keep from the work:**

- `tools/diagnose_reasoning_model.py` + `tools/sweep_reasoning_fixes.py`
  stay as reference diagnostic tooling; the principle of
  raw-probe-before-blocklist (see
  `feedback_diagnose_before_blocklist`) was validated even
  though it didn't recover the model.
- `OllamaAdapter` + `OllamaAssistantAdapter` `force_json` kwarg
  + `bypass_suitability_check` kwarg stay shipped — useful
  escape hatches for future investigation, no v1.0 production
  code-path uses them. The `--force-json` / `--bypass-suitability-check`
  flags on the probe scripts remain useful for diagnostic
  re-evaluation if a future model release looks promising.
- `config/prompts/*-compact.md` stay as documented worked
  examples of "compact-prompt design pitfalls" — drop framing
  prose but keep constraint clauses (the failure analysis in
  the memory `feedback_compact_prompts_preserve_constraints`).
- The sweep records at
  `docs/reference/sweep-2026-05-25-reasoning-models.md` (now
  includes the 2026-05-26 v2 follow-up section) stay as the
  permanent forensic record.

**What we drop:**

- The previously planned `force_json_output` per-(model, role)
  config field. The two configs where it'd matter
  (`phi4-reasoning:14b-plus` advisor, `deepseek-r1` operator)
  don't justify the per-model config surface area when neither
  model is a recommended pick in its respective role.
- Future second-pass compact-prompt iterations against
  phi4-mini-reasoning. Two attempts, both failed; further
  iteration is not warranted.
- The "Slice 3 / Slice 4" plan from the original entry — both
  cancelled.

**Re-opening this:** any future small (sub-7B) reasoning model
release that the operator wants to evaluate. Use the existing
`tools/diagnose_reasoning_model.py --model <tag>` harness; if
direct-probe output looks promising, a sweep run can establish
the score. Re-opening is a new investigation against new model
data, not a continuation of this thread.

### Foreign-language operator support -- audit + test coverage

**What:** wobblebot is end-to-end English today, with no
visibility into how the operator-assistant LLM, embed renderer,
status_report narrative, or compatibility matrix behave under
non-English operator input. A non-English-speaking operator's
experience is currently undocumented and untested.

**Surfaces affected:**

1. **Operator-assistant prompt** -- `config/prompts/operator.md`
   is entirely in English. It doesn't tell the LLM "the
   operator may write in any language; parse the intent even if
   the input is Spanish / French / German / Mandarin / etc.
   while emitting JSON in the catalog's English ``kind`` values."
   Most modern LLMs (llama3.2, qwen2.5, gemma2, mistral) have
   multilingual training and SHOULD handle this gracefully, but
   the prompt doesn't acknowledge or test it.
2. **status_report narrative** -- LLM-generated prose is in
   whatever language the model prefers given the prompt
   (currently English-only). Operator can't ask for a Spanish
   brief.
3. **Embed labels** -- "Engine status", "Today's PnL",
   "Recent fills", "No fills in the lookback window", etc. all
   hardcoded English in the renderer.
4. **Compatibility matrix** -- the 14-message routing battery
   in `tools/probe_assistant.py` is entirely English. We have
   ZERO data on how our chosen Ollama models handle non-English
   operator input.
5. **News pipeline** -- already ingests non-English headlines
   from CryptoCompare without sanitization; no specific
   handling but no breakage observed either.
6. **`operator-llm-models.md` doc** -- caveats only mention the
   English-only test battery in passing; the compatibility
   scores don't tell a German operator anything useful about
   their experience.

**Implementation paths (pick at scoping time):**

1. **Audit only** -- run the routing battery in 3-5 target
   languages (e.g. Spanish, French, German, Japanese, Mandarin)
   against the top-scoring English models from
   `docs/reference/operator-llm-models.md`. Document the
   accuracy delta. Cheapest path; identifies whether the
   English-only assumption is actually load-bearing.
2. **Prompt expansion** -- update `operator.md` with explicit
   "respond in the operator's language for conversational /
   unparseable replies; keep JSON ``kind`` values English"
   language. Add a few non-English routing examples. Rerun the
   compat matrix.
3. **i18n the embed labels** -- introduce a small string
   catalog (Python ``gettext`` or a simple dict) and let
   operators set `web.locale` / `operator.locale` in
   `settings.yml`. Mostly mechanical translation work but
   touches every renderer.
4. **Full multilingual status_report** -- localize the
   narrative section's system prompt so the LLM outputs prose
   in the operator's language. Combines with #2 and #3.

**Why deferred:** zero evidence yet that any operator wants
this. The v1.0 operator is English-speaking. Audit-only
(option 1) is cheapest to disprove or confirm the need; the
fuller paths (#2-#4) wait for community signal.

**Trigger:** any operator (community or otherwise) asking for
non-English support, OR observed news-pipeline garbling on
non-Latin character sets (would be a different fix path but
same i18n umbrella). Operator-flagged 2026-05-25 ("we have yet
to test foreign language capabilities in any part of this
project") -- noted as a known gap, not an immediate priority.

**Cross-references:**

- `docs/reference/operator-llm-models.md` -- add a methodology
  note that the routing battery is English-only.
- `tools/probe_assistant.py` -- DEFAULT_BATTERY is English; a
  non-English variant would be a sibling battery.
- `config/prompts/operator.md` -- the language-agnosticism
  clause goes here when path #2 lands.
