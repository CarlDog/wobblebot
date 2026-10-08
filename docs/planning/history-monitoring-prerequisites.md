# History and monitoring prerequisite map

This is preparation for a possible next-version scope decision, not adoption of
2.2/2.3 or an accepted Historian/anomaly design. The branch starts from the
unmerged 2.1 candidate `a89d017d804dd0eae8dd16804aa2d71d7b38ee0f`; its inherited
changes require the existing PR #170 review. Version assignment remains open.
Current completion status belongs in [the roadmap](roadmap.md).

The branch has since incorporated merged main `6c108ab` without rewriting its
earlier preparation commit. The [offline timestamp inventory](../implementation/history-coverage.md)
supports the G3 evidence-acquisition step; it does not establish source provenance,
price quality, canonical scoring or gate acceptance.

## Source authority and dependencies

The [backlog G1/G3 rows](backlog.md#gated-work) control eligibility. Historical
calendar triggers in the [observability catalog](../release/v1.1/observability.md)
do not establish usable data. [P4.6 and the standing scoring thread](p4-completion-plan.md)
require verified imports and canonical scoring before an accepted Historian
design. The [OpenChronicle assessment](../reference/openchronicle-repository-assessment-2026-08-29.md)
is design input only; it does not adopt a runtime integration.

| Work | Documented prerequisite / acceptance | Existing substrate | Evidence still required |
| --- | --- | --- | --- |
| G1 behavioral monitoring | Per-signal coverage, gaps and retention; usable baseline and accepted consumer/design | Stored trades, balances, suggestions, LLM calls and transfer proposals; N4 liveness/doctor | Approved historical snapshot, retention/planned-outage context, signal-specific sufficiency and false-positive review; named notification consumer |
| G1 disk awareness | Agreed anomaly/operations consumer | N4 doctor exposes current disk diagnostics | Consumer, threshold/repeat policy and response contract; current free space alone is not a historical baseline |
| G3 import readiness | Q2 dump availability and verified imports; six live symbols at 1h plus BTC at 1m | `tools/import_kraken_history.py`, idempotent bars/snapshot writes | Verified archive provenance, actual configured symbol list, coverage/gap and rejected-row receipts against the canonical store |
| G3 outcome readiness | Canonical NAS scoring; pending, missing-bar and unscoreable tallies; 1m/60m fidelity and selection-bias analysis | `tools/score_recommendations.py`, `tools/score_report.py`, evaluator and scoreboard services | Authorized NAS tools execution, before/after backup and database identity, actual scored corpus and paired comparisons |
| P4.6 Historian | Own accepted design after the scored-ledger gate; read-only first | Existing canonical SQLite storage, outcome ledger and web architecture | Decide model/cost, cadence, eligible corpus, provenance, bounded retrieval, findings lifecycle and privacy; then separately implement findings and `/historian` |

## Bounded preparation sequence

1. Obtain an approved read-only evidence lane and snapshot inventory. Record file
   identity, capture time, schema version and coverage without committing private
   data. A reachable VPN alone is neither data availability nor write authority.
2. Inspect each proposed G1 input separately. Record usable intervals, sample
   counts, retention boundaries and known gaps. Do not reconstruct cancellation
   events from upserted current order rows. Label unknowns; elapsed days are not
   evidence of usable observations.
3. Check the actual historical archive against the required symbols/intervals.
   Verify importer behavior on a disposable copy before any separately authorized
   canonical import. Preserve invalid-row counts and missing windows.
4. Use the existing scorer's documented 60m corpus, 1m BTC and directional
   namespaces under the ratified outcome policy. A local fixture run demonstrates
   code behavior only; it cannot replace the required canonical NAS run.
5. Report scored, pending, bars-missing and permanently unscoreable cases. Compare
   fidelity only on paired eligible rows. Preserve sample sizes, evaluator
   version, routing and selection bias; hit-rate and rank are not dollar profit.
6. Present the resulting evidence with the smallest remaining design decisions.
   Start detector/Historian runtime implementation only after its specific gate
   is met. New paid inference, deployment and persistent-store mutation retain
   their separate authorization requirements.

## Inferred verification checks for the eventual designs

These are proposed engineering checks, not additional adopted acceptance terms:
missing/retained-away input must report unknown rather than healthy; planned
outages must not silently train abnormal baselines; alert deduplication must
survive the chosen lifecycle without unlimited repeats; findings should retain
source/evaluator identity and distinguish observation from inference; retrieval
must preserve canonical eligibility before limits and respect retention.

Reuse the shipped importer/evaluator/scoreboard and N4 health components. Do not
add a duplicate readiness service, embeddings, OpenChronicle runtime dependency,
or automatic trading response merely to prepare a future version. Real equities
remain disabled and deferred under ADR-055.
