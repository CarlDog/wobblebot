# Offline historical timestamp coverage

`tools/check_history_coverage.py` supplies bounded evidence for the G3 import/
coverage prerequisite in [the backlog](../planning/backlog.md#gated-work) and
[P4's canonical scoring sequence](../planning/p4-completion-plan.md#the-standing-external-thread-interleaves-at-any-point).
It is a standalone inspection tool, not a daemon, importer, scorer, anomaly
detector or Historian. This preparation does not adopt a 2.2/2.3 product scope.

Use an explicitly approved, consistent SQLite snapshot containing `ohlc_bars`:

```sh
python tools/check_history_coverage.py \
  --db /path/to/approved-observe-snapshot.db \
  --symbols BTC/USD \
  --interval-minutes 1 60 \
  --start 2026-04-01T00:00:00Z --end 2026-07-01T00:00:00Z
```

Select the actual configured symbols when collecting evidence; this example
does not identify the operator's current universe. G3 requires six live symbols
at one hour and BTC at one minute. Run those as separate selections to avoid
inventing a one-minute coverage requirement for every symbol. Dates above are
an explicit Q2 example, not a claim that the dump is available or imported.

The tool opens the supplied file with SQLite `mode=ro`, enables `query_only`,
and uses one read transaction across selected series. It does not load operator
configuration, contact providers, create/migrate databases, import bars or score
outcomes. Snapshot provenance is not independently verified; retain the approved
snapshot's capture/source/backup identity alongside the output. Never substitute
a synthetic fixture report for canonical NAS evidence.

## Interpretation

The supported intervals are G3's one-minute and 60-minute bars. The expected grid
is aligned to UTC epoch minute/hour boundaries within `[start, end)`. Both bounds
must include an offset and align to every selected interval. Offset timestamps
are normalized before counting. Multiple rows at the same normalized slot count
once and increment the duplicate counter; fractional/off-grid timestamps do not
fill expected slots. Naive or unparseable timestamps are invalid, never silently
assumed UTC.

Each selected series is scanned in full so lexical date filtering cannot hide
malformed or offset timestamps. Invalid-timestamp counts cover the **entire selected
series**, while duplicates/off-grid counts cover the requested window. Outside-window
rows are counted separately. Missing ranges use an exclusive end and report their
full count even when the listed examples are truncated.

Missing slots do not distinguish listing windows, zero-trade/provider omissions,
retention, collection outages or failed imports. This report does not validate
OHLC values, import provenance, trade counts, retention policy, evaluator eligibility,
selection bias or profitability. `readiness_gate_cleared` is always false.

- Exit **0**: complete requested timestamp grid with no reported timestamp defects.
  This is not production-data or readiness approval.
- Exit **1**: a complete inspection found gaps, duplicates, off-grid or invalid
  timestamps. Review the structured report and compare with source evidence.
- Exit **2**: invalid input, unreadable/missing schema or exhausted scan budget;
  no partial JSON report is emitted. A missing input file is never created.

## Bounds and evidence acquisition

Defaults: one million scanned rows across all selections and 30 seconds. The
operator may explicitly raise `--max-rows` (maximum ten million) or
`--timeout-seconds` (maximum 300). SQLite execution and row processing both check
the deadline. At most 32 symbols, one million expected slots per series and two
million aggregate slots are allowed. Output lists at most 20 missing ranges per
series by default. Larger historical stores may exceed the row budget because
all timestamps in each selected series are inspected. Use a narrower approved
snapshot or deliberate bounded rerun; do not interpret an aborted scan as coverage.
OS-level filesystem stalls still require the caller's external command timeout.

The acquisition sequence remains: obtain approved consistent snapshot/source
identity; inspect coverage; reconcile omissions with the archive and retention
context; separately authorize canonical imports/scoring; collect pending,
bars-missing and unscoreable tallies and fidelity/selection-bias analysis; then
settle the consumer and Historian design. The tool performs only the timestamp
inventory step. G1's per-signal baseline and consumer decisions are unchanged.
