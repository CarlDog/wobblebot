# Probe-Battery Punch List — fix before the next seat campaign

Defects in `tools/probe_news.py` / `tools/probe_risk.py` (and shared
plumbing) found by the 2026-08-17 adversarial audit of the Phase B
16-model matrix. **None of these biased the Phase B run itself** — the
audit verified that from the run's ground truth (caps 60/8 vs $12.79
spent, zero UNSAFE verdicts, all 96 sections rc=0) — but several would
bias a future run under different conditions, and two actively corrupted
one model's scores. Ordered by severity. The register's rule 4 ("errors
are not verdicts") is the theme: infrastructure and judgment must stop
sharing a scoreboard.

## HIGH — would change a future seat decision

1. **ERROR must become its own axis (availability), never a judgment
   deduction.** Both batteries subtract a point for a timeout, empty
   response, truncation, parse failure, or cost-gate denial —
   indistinguishable in the totals from a wrong call. Phase B receipt:
   qwen3.5-flash answered 36 news fixtures, got 36 right, and scored
   36/66 — *below the battery's printed constant-HOLD floor* — because
   ~45% of its responses arrived empty. Report `answered`, `correct`,
   and `errored` separately; rank on correct/answered with answered/total
   alongside. Keep the SUMMARY's raw ERROR count, but stop folding it
   into `non-unsafe N/18`.
2. **`probe_news.py` crashes on a cost-cap trip with the scorecard
   unprinted.** Its per-fixture catch is `except AdvisorError` only;
   `LLMCostCapExceeded` subclasses `WobbleBotDomainError` and escapes.
   `probe_risk.py` catches both. Align news with risk (catch → ERROR
   row), and per item 1, report it as availability.
3. **The shared cost gate makes multi-model sweeps order-dependent.**
   The daily cap is a single pool over the whole probe ledger, checked
   against a worst-case estimate (`tokens_out = max_tokens`), so late,
   expensive models inherit earlier models' spend and get denied while
   cheap ones sail. Phase B dodged it only because `run_matrix.py`
   passed `--daily-cap 60`. For sweeps: per-model session caps as the
   budget, daily cap as the runaway backstop (already the run_matrix
   convention — document it in both tools' `--help`), and log any
   denial loudly as an availability event.
4. **Token budget vs reasoning models.** Uniform `--max-tokens 4000`
   truncated qwen3.8-max mid-JSON (unterminated object at ~530 chars
   after 85s — reasoning burned the budget; the CLAUDE.md audit-item
   trap, third occurrence in the project's records). No adapter checks
   `finish_reason`/`stop_reason`, so truncation reads as malformation.
   Detect and report truncation distinctly; consider per-model budget
   overrides for reasoning-heavy models.
5. **Temperature parity.** The OpenAI adapter silently drops
   `temperature` for `^(o\d|gpt-5)` models and the Anthropic adapter for
   Claude-5-generation models, while Google/Ollama/Atlas-served models
   sample at the battery's 0.6/0.4. Round-to-round variance is therefore
   a property of the harness branch, not the model. At minimum, log the
   effective temperature per run in `JSON_RESULT`; ideally pin
   temperature 0 where every provider honors it, or report variance only
   within same-branch comparisons.

## MEDIUM — weakens confidence in close calls

6. **Risk deadband grades timid-but-correct as UNSAFE.** A +4.2% spacing
   widen or a -5.0% size cut on a severe fixture classifies as `hold` →
   UNSAFE — the battery's worst verdict for a directionally correct,
   merely small, move. The sibling quant battery explicitly engineered
   this dead zone out. Zero Phase B models were bitten (nobody was
   timid), but the first small-nudge-style model will be mislabeled
   dangerous. Grade direction and magnitude separately.
7. **Malformed numerics: `probe_risk` silently coerces to `hold` (→
   potentially UNSAFE); `probe_news` labels the same input
   `BAD_VALUE`.** Align on the news behavior — a formatting failure is
   an availability event (item 1), never a safety verdict.
8. **The echo path skips the `empty_window` low-confidence check.** The
   omitted-hold path enforces `expect_low_confidence`; the
   echoed-current-value path returns OK without checking confidence.
   Exercised 6 times in Phase B (doubao, gpt-5.4-nano — worth up to
   +1/22 per round). Apply the same confidence check on both hold paths.
9. **Last-JSON-wins parsing + a trailing example in both prompt files.**
   `news.md` and `risk.md` each end with an example JSON whose values
   would grade as *action*; a cloud model restating the template after
   its answer gets the example graded instead of its call. The Ollama
   split-fields path already mitigates this by field ordering; the cloud
   path has no equivalent. Either strip/neutralize the trailing examples
   (per the compact-prompts rule, keep the constraint, change the
   values to obviously-inert ones) or prefer the first schema-valid
   object that differs from the documented example.
10. **Contested fixture labels are doing most of the discrimination.**
    `bullish_rally_coverage` (24/48 news failures matrix-wide) and
    `fresh_drawdown_light_exposure` (28/48 risk failures) account for
    the bulk of top-of-field separation, and both labels are defensibly
    arguable from the prompts' own words (`distant_macro_event` and
    `favorable_regulatory_clarity` carry similar tension). Per the
    `single_denied_rumor` precedent: fix by making the *evidence*
    dispositive in the fixture text, never by relabeling after watching
    who fails. Until then, conclusions must hedge on these fixtures
    explicitly.

## LOW — hygiene

11. **ERROR rows fabricate `dir=hold`** in `probe_risk` — record
    `direction=None` so tallies of the direction column stay honest.
12. **`JSON_RESULT` is too thin for post-hoc audit** — include the
    model's actual emitted values (spacing/size/confidence), the
    effective temperature, and untruncated error causes (risk currently
    truncates to 60 chars), so the next red-team doesn't need the log
    stream.
13. **`probe_risk` cloud rows are ledger-tagged `role="quant"`** —
    cosmetic today, but it makes sweeps indistinguishable in the shared
    pool that item 3 cares about.

## Non-goals

- **Do not relabel the contested fixtures to match Phase B outcomes** —
  that is teaching to the test (the `rumor_debunked_on_chain` revision
  note shows how carefully this must be done).
- **Do not add rounds to compensate for the ceiling.** Misses cluster
  by fixture; more rounds re-measure the same disagreement. The ceiling
  fix is harder fixtures (gen3-class), and only if a unique seat-holder
  is actually needed.

Source: three isolated adversarial reviews (harness audit, methodology
audit, per-fixture forensics) run 2026-08-17 against the Phase B tier
logs and tool source; synthesis in `advisor-seats.md` § "Full-field
matrix (Phase B)".
