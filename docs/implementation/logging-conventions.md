# Logging Conventions

Ratified with the P3 logging-quality audit (2026-08-09). The global
rule was always "operator-facing data in the message string,
structured fields in `extra=`" (CLAUDE.md); this note makes the
application concrete after three incidents where the rule's absence
had real diagnostic cost.

## The rules

1. **The message string must answer what / which / how-much on its
   own.** The default plain formatter renders message-only — anything
   living exclusively in `extra=` is invisible to an operator tailing
   the container log. `extra=` duplicates the fields for JSON
   consumers; it never replaces them.

   ```python
   # NO — which symbol? how many placed?
   _LOGGER.info("grid re-layout complete", extra={...})

   # YES
   _LOGGER.info(
       "grid re-layout complete for %s: placed %d/%d%s",
       symbol, placed, target, f" ({refusals} refused)" if refusals else "",
       extra={...},
   )
   ```

2. **Lazy `%s` interpolation, not f-strings**, for the message args
   (defers formatting when the level is disabled; keeps pylint's
   `logging-fstring-interpolation` clean).

3. **Every state-change, money-touching, or external-call line names
   its entity**: symbol for engine events, exchange_id for orders,
   proposal_id for treasury, daemon name for lifecycle.

4. **Severity states the truth**: expected degraded states are INFO
   with context (partial layout, parked offside heartbeat); WARNING
   means something needs an eye; per-item routine refusals may be
   DEBUG **only when** an INFO summary carries the counts (the
   partial-grid pattern) — a DEBUG demotion without a summary is how
   the zero-order re-layout loop became invisible.

5. **Exceptions go in the message too** — `str(exc)` in `extra=`
   alone is swallowed by plain format, and some exception types
   (httpx `ReadTimeout`) have empty `str()`: include
   `type(exc).__name__` when the class carries the signal.

6. **Transition + heartbeat pattern for persistent states** (offside,
   wide spread, sell-guard defers): one WARNING on entry, a periodic
   INFO summary with the consecutive count, one INFO on recovery —
   never a WARNING every tick.

## Why (the incident receipts)

- **2026-06-03 (soak):** bare `grid fill` lines — no symbol, side, or
  price — made "6 fills in 17h" unreadable from the tail.
- **2026-08-09 (re-anchor e2e):** symbol-less `grid offside; parking`
  / `no open orders detected` lines made it impossible to tell WHICH
  of six symbols was busy-looping; the DEBUG-demoted refusals plus a
  count-less completion line hid that a re-layout placed 0/6.
- **2026-08-09 (cold-start timeout):** the assistant-parse error only
  became diagnosable after the exception text moved into the message
  (`assistant parse failed: AssistantError: … ReadTimeout:`).

## Audit state

Installment 1 (2026-08-09) enriched the engine path — `grid_engine`,
`reconciler`, `cost_basis` — the tail the operator actually watches
during live trading.

Installment 2 (2026-08-10) took the **always-on daemons and the money
path**, at the severities an operator acts on (WARNING / ERROR /
EXCEPTION): `cli/harvest_execute` (every withdrawal refusal now names
the proposal and the numbers), `cli/harvest`, `cli/live`,
`cli/operator`, and `cli/_common`'s shared lifecycle. Scope was chosen
by asking "if this fires at 3am, can the operator act on the line
alone?" — which is why the one-shot CLIs (`preflight`, `recalibrate`,
`apply`, `shadow`) and the web routes wait for installment 3, along
with the INFO/DEBUG tier everywhere.

A rule-1 violation has a **mechanical signature** — a static message
paired with a non-empty `extra=` — so it is greppable rather than a
matter of taste. That scan counted 239 before installment 2 and 165
after; the remainder is the deliberate scope boundary above, not
backlog rot.

Both scans live in **`tools/scan_logging.py`**:

```bash
python -m tools.scan_logging                 # rule 1 — exits 1 on any hit
python -m tools.scan_logging --check decimal # readability review list
python -m tools.scan_logging --check all --verbose
```

Installment 2 also added `cli/_common.fmt_decimal` (a rule-1
corollary): storage and Kraken return full-scale Decimals, so `%s`
printed `342.18000000` for a $342.18 withdrawal and — worse — `1E+2`
for a round $100. Money lines are exactly where a number must be
scannable at a glance, and E-notation in a withdrawal log is a real
misread risk. It strips trailing zeros WITHOUT forcing a scale, so it
stays honest for asset amounts (a 2dp quantize would render a live BTC
quantity as `0.00`).

Installment 3 (2026-08-10) finished the sweep: every remaining module
and every severity, including the one-shot CLIs and the web routes.
**The rule-1 scan now reports zero.**

It also added the SECOND scan the first one structurally cannot do
(`--check decimal`).
Rule 1 finds *missing* data; it cannot find *unreadable* data — a line
that correctly interpolates `assessment.average_cost` still printed
`73390.78543435964243143764881`, because a `Decimal` division keeps 28
significant digits. That was live in production on 2026-08-10 and the
rule-1 scan was blind to it by construction. The Decimal scan flags a
money-ish expression interpolated without a formatter; it is a review
list, not a gate (ints and `.total_seconds()` floats trip it).

`fmt_decimal` gained `max_significant` for exactly that case: a
division result has no trailing zeros to strip, so capping significant
digits is the only thing that helps. Capping *significant digits*
rather than decimal places keeps one setting usable across magnitudes
— 10 digits gives `73390.78543` for a BTC price and `0.0694757` for a
DOGE one, where a fixed 2dp would destroy the latter.

**Two lessons from the mechanical passes**, worth honoring next time:

1. A transformer that appends *every* `extra=` field produces 8–11
   `key=%s` pairs — a wall of text, which is just a different way of
   being unreadable. In-message context is capped at 4 fields; the rest
   stay in `extra=`, which is what `extra=` is for.
2. `fmt_decimal` started in `cli/_common` and had to move to `domain`
   the moment `services/cost_basis` needed it — services cannot import
   cli. Put a shared display helper in `domain` from the start.

**The rule-1 scan is enforced, not just documented.**
`tests/tools/test_scan_logging.py::TestPackageIsClean` asserts the
package reports zero, so a regression fails the suite rather than
waiting for the next audit. Fix the log line, not the test.

New code follows these rules from day one. Message-prefix pin tests
live in `tests/services/test_grid_engine.py::TestPartialGridPlacementLogging`;
`fmt_decimal` is pinned by `tests/domain/test_fmt_decimal.py`.
