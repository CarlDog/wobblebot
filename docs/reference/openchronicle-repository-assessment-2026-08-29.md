# OpenChronicle repository assessment — 2026-08-29

**What this is.** A source-backed assessment of
[`CarlDog/openchronicle-mcp`](https://github.com/CarlDog/openchronicle-mcp) and the storage,
retrieval, maintenance, and provenance patterns that could improve WobbleBot. The purpose is to
preserve implementation-quality findings for later review without adopting OpenChronicle as
WobbleBot's financial database, generic memory layer, or agent runtime.

**Status.** External reference assessment only. It is not an ADR, implementation commitment,
security certification, or project-status document. Binding architecture decisions remain in
[`decisions.md`](../architecture/decisions.md) and
[`ratified-decisions.md`](../architecture/ratified-decisions.md); sequencing and completion status
remain solely in [`roadmap.md`](../planning/roadmap.md). P4.6 Historian remains behind its existing
data and design gates.

**Reviewed baselines.**

- OpenChronicle `main` / `v3.3.0` at
  [`7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45`](https://github.com/CarlDog/openchronicle-mcp/tree/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45),
  reviewed 2026-08-29.
- WobbleBot `main` at `2645c437a98696c5d1090b3685876999a290bbb7`.

**Method.** Read-only source review of both repositories, their architecture and security
documentation, schemas, migrations, search code, maintenance paths, tests, and roadmap gates. A
bounded dogfood query used the existing OpenChronicle WobbleBot project corpus without writing to
it. A local OpenChronicle test run reached 842 passing tests; its two remaining CLI smoke tests
were blocked when the managed Windows environment denied their test-created `git commit`
subprocesses. Neither failure exercised storage or retrieval logic. This was not a production
benchmark, penetration test, legal review, or WobbleBot implementation pass.

---

## Executive verdict

Do **not** use OpenChronicle as WobbleBot's canonical durable store, embed it in-process, expose
its full MCP surface to WobbleBot's LLMs, or add a general plugin/memory subsystem around it.

OpenChronicle v3 is a focused, single-user memory service with useful engineering patterns. Its
record, trust, and deletion semantics are deliberately much weaker than WobbleBot's financial and
forensic requirements. A memory contains content, tags, timestamps, a pin, project, and a
transport-oriented source; it does not provide authenticated claim provenance, an immutable event
chain, retention/expiry, supersession, actor identity, or scoped read-only credentials. Its
[security posture](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/docs/configuration/security_posture.md)
explicitly describes a single-user deployment with optional authentication and no audit log.

The useful transfer is narrower:

1. Correct WobbleBot's current **filter-after-limit** query defect.
2. Strengthen backup/archive publication, backup ordering, and maintenance success visibility.
3. Use versioned migration discipline when the next substantive schema is introduced.
4. Build the future Historian as WobbleBot-owned canonical findings plus a rebuildable lexical
   search index.
5. Consider embeddings only after a measured FTS5 retrieval failure, with full derived-artifact
   identity and stale-publication protection.

OpenChronicle may remain useful to development agents as an external working-memory service. That
is separate from WobbleBot runtime architecture and grants it no financial authority.

---

## Controlling WobbleBot boundaries

This assessment does not reopen these decisions:

| Source | Constraint on this assessment |
|---|---|
| ADR-001 in [`decisions.md`](../architecture/decisions.md) | Ports and adapters remain the architectural boundary. |
| ADR-002 | LLM output remains advisory; confirmation and typed command handling remain the execution firewall. |
| ADR-003 / ADR-004 | Harvester alone owns withdrawal authority; a search or memory component receives no transfer capability. |
| ADR-007 / ADR-012 | Advisor output remains schema-validated, range-bounded, and structurally separated from news-driven auto-application. |
| ADR-013 | Raw operator turns remain in `operator.db`; the assistant parses intent but never executes. |
| ADR-014 | Cloud LLM inference remains cost-accounted. Any cloud embedding path needs a separately ratified, ADR-014-compatible cost and egress design. |
| ADR-035 | Any advisor-memory experiment must be evaluated against persisted, versioned outcomes rather than model confidence. |
| ADR-036 | Retention and forensic-table exclusions remain authoritative; a mirror may not silently extend raw-data retention. |
| ADR-041 | A search sidecar or indexer receives only its declared credentials and mounts, never broad WobbleBot capabilities. |

The release plan also explicitly defers embeddings/RAG behind a measured retrieval failure and
keeps P4.6 behind the Q2 corpus, canonical scoring, and its own design document
([`release-2.0-plan.md`](../planning/release-2.0-plan.md)). Writing this assessment does not satisfy
those gates.

---

## WobbleBot capabilities that should not be rebuilt

OpenChronicle does not reveal a need to replace WobbleBot's persistence layer. Important existing
strengths are:

- [`SQLiteStorageAdapter`](../../src/wobblebot/adapters/sqlite_storage.py) already enables foreign
  keys, WAL, `synchronous=NORMAL`, and a bounded WAL high-water mark for on-disk databases.
- Its read-only mode uses SQLite `mode=ro`, skips schema/migrations, and cannot silently create or
  mutate a database another daemon owns.
- [`sqlite_migrations.py`](../../src/wobblebot/adapters/sqlite_migrations.py) is additive,
  idempotent, tolerant of multi-daemon startup races, and refuses to destroy potentially forensic
  duplicates.
- [`backuper.py`](../../src/wobblebot/services/backuper.py) already uses SQLite's online backup API
  and performs a deeper restoration smoke test with `PRAGMA integrity_check` plus reads from every
  user table.
- [`retention.py`](../../src/wobblebot/services/retention.py) archives before deletion, permits only
  explicitly registered tables, and excludes forensic ledgers.
- `advisor_suggestions` retains exact input summaries, model identity, rationale, expert opinions,
  and the structural news flag; `recommendation_outcomes` is evaluator-versioned and append-only
  for a given scoring identity
  ([`sqlite_storage_schema.py`](../../src/wobblebot/adapters/sqlite_storage_schema.py)).
- Conversation history is already strictly scoped by `(channel_id, user_id)` and bounded to the
  most recent turns. That is appropriate intent context, not a reason to import generic memory.

The recommendation is therefore incremental hardening plus a dedicated future search boundary,
not a database rewrite.

---

## Disposition summary

| Area | Evidence | Disposition |
|---|---|---|
| Recent-suggestions symbol filtering | Confirmed current correctness defect | Fix independently of P4.6; highest-value immediate transfer |
| Backup publication and selection | Consistent online snapshot already exists; final artifact is published directly; “latest” sorts by mtime despite filename time being authoritative | Candidate near-term durability and correctness hardening |
| Archive publication | Archive-before-delete exists; a crash can leave a partial final-name archive that blocks retry | Candidate near-term durability hardening |
| Maintenance health | Daemon heartbeat can stay fresh while an individual job repeatedly fails | Candidate observability hardening |
| Numbered migration ledger | Current migrations are strong but unversioned and rerun on each writable connect | Adopt with the next substantive schema; do not rewrite solely for style |
| News/operator lexical search | Current reads are structured/time-based; web news filtering scans a capped Python slice | Bounded FTS5 pilot candidate |
| P4.6 Historian storage | Existing gate and provenance rules already control the work | WobbleBot-owned canonical schema when gate opens |
| Semantic/hybrid retrieval | No measured WobbleBot lexical-retrieval failure | Conditional experiment only |
| OpenChronicle runtime backend/plugin | Weak provenance/retention/auth fit; AGPL/Python mismatch | Decline |
| Raw transcript/news/tool-output ingestion | Retention, privacy, replay, and prompt-injection risk | Decline |
| Generic memory injection into advisor/intent parsing | Creates an untrusted path toward commands or auto-apply | Decline |

---

## Confirmed finding 1 — recent suggestions filter after the result limit

[`RecentSuggestionsQuery`](../../src/wobblebot/ports/operator_intents.py) asks for the most recent
advisor suggestions and supports both an optional symbol and a bounded limit. The current service
implementation in [`operator_service.py`](../../src/wobblebot/services/operator_service.py):

1. requests `limit=query.limit` rows from storage;
2. derives each symbol from `input_summary`; and
3. removes rows whose symbol does not match.

This answers “matching rows among the newest N overall,” not “the newest N matching rows.” If the
five newest suggestions are ETH, `recent_suggestions(symbol=BTC, limit=5)` can return empty even
when an older BTC suggestion exists.

OpenChronicle encountered the same general retrieval class: filters applied after a candidate
window let out-of-scope rows consume all top-N slots. Its memory port therefore makes
[eligibility available before semantic
selection](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/domain/ports/memory_store_port.py#L161-L189),
and its FTS path applies [project/tag predicates before the
limit](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/sqlite_store.py#L837-L861).

**Narrow recommendation.** Make symbol a storage-level predicate applied before ordering and
`LIMIT`. Do not compensate by guessing an over-fetch multiplier; that remains incorrect when a
run of other-symbol rows exceeds the multiplier.

**Acceptance evidence if scheduled:**

- With N newer wrong-symbol rows and an older matching row, `limit=1` returns the matching row.
- No-symbol queries preserve current newest-first behavior.
- Symbol normalization matches the domain `Symbol` representation used by the query model.
- The predicate is evaluated in SQL before `LIMIT`, with an appropriate index or documented JSON
  extraction plan.
- Adapter and service tests pin the semantics separately.

This is ordinary query correctness and does not need the Historian gate to open.

---

## Candidate improvement 2 — verify before publishing backups and archives

WobbleBot's backup operation uses SQLite's online backup API, so the database snapshot is
consistent while writers continue. It opens the final timestamped destination directly, writes
the backup, and removes it after a caught SQLite error. A process termination, host reset, or I/O
failure outside that caught path can nevertheless leave a partial artifact at the final name.
The separate deep verification job may not run until its later cadence.

OpenChronicle's backup module uses a stronger publication protocol
([source](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/backup.py)):

```text
online SQLite backup
  -> sibling temporary file
  -> reopen read-only
  -> PRAGMA quick_check
  -> atomic replace into final name
```

An invalid staged file is quarantined outside the normal retention glob rather than deleted as if
nothing happened. The published artifact is therefore always a validated, openable SQLite file.

The same principle applies to WobbleBot's CSV/GZip archives. Current archive-before-delete
discipline protects source rows, but a crash during direct final-name writing can leave a partial
archive. A later run refuses to overwrite that filename and requires manual intervention.

There is also a current backup-ordering inconsistency in
[`backuper.py`](../../src/wobblebot/services/backuper.py). The shared “newest first” helper sorts
by filesystem mtime, while `parse_backup_timestamp` explicitly identifies the timestamp embedded
in the filename as authoritative because a copy or rsync can refresh mtime. Backup pruning,
same-day dedupe, and restoration verification can therefore select a different file from the
chronologically newest backup.

**Narrow recommendation.** Stage backups and archives beside their final target, validate/close
them, then publish with `os.replace`. Retain WobbleBot's deeper scheduled restoration verification;
`quick_check` at publication complements rather than replaces it. Run a successful backup as an
in-code prerequisite to `VACUUM` rather than relying on independent schedules to happen in a
safe order.

**Acceptance evidence if scheduled:**

- Final-name files become visible only after validation succeeds.
- A failed staged SQLite file cannot become `find_latest_backup()`'s answer.
- An invalid staged backup is preserved for forensic inspection without entering retention or
  restore selection.
- Latest selection, verification, dedupe, and retention use the authoritative filename timestamp;
  copied/refreshed mtimes cannot reorder conforming backups.
- Malformed backup names have an explicit policy and cannot silently outrank valid timestamped
  backups.
- A crash/fault injected during archive writing leaves source rows untouched and permits a clean
  retry without manual deletion of a final-name partial.
- Windows rename behavior is tested after every SQLite/GZip handle is explicitly closed.
- `VACUUM` refuses or skips safely when its prerequisite backup fails.

---

## Candidate improvement 3 — distinguish maintenance attempts from success

`cli/maintenance` emits its daemon heartbeat at the start of vacuum, prune, backup, verification,
reconciliation, capital, and ledger cycles. Per-target failures are logged and several critical
failures notify the operator, but the shared daemon heartbeat can remain fresh while one job has
not succeeded for days.

OpenChronicle tracks [separate per-job
state](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/application/services/maintenance_loop.py#L47-L72):

- `last_run_at` / last completed attempt;
- `last_success_at`;
- last outcome and error; and
- total, successful, failed, and overlap-skipped counters.

It [persists only `last_run_at` and
`last_success_at`](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/application/services/maintenance_loop.py#L265-L335);
outcome, error, and counters are runtime-only. The implementation explicitly
[avoids advancing `last_success_at` on
failure](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/application/services/maintenance_loop.py#L204-L241).

**Narrow recommendation.** Add a small durable per-job status model or equivalent bounded
operator-state record. Daemon liveness and task success answer different questions and should be
shown separately. This should reuse WobbleBot's health presentation rather than create a second
scheduler or monitoring stack.

**Acceptance evidence if scheduled:**

- A frequently attempted but permanently failing backup job shows live daemon / failed job.
- Last success survives daemon restart.
- One task's success cannot mask another task's failure.
- Status records contain no credentials, financial payloads, or raw exception tracebacks.
- Existing notifications remain the active alert path; the ledger supplies durable diagnosis.

---

## Candidate improvement 4 — version future schema changes explicitly

WobbleBot currently applies the complete declarative `SCHEMA`, then calls each additive migration
function in a fixed Python sequence on every writable connection. The migration helpers are
careful and fit WobbleBot's multi-daemon startup behavior, but the database cannot answer which
migrations were applied, when they were applied, or whether an already-applied migration file was
later changed.

OpenChronicle uses monotonically numbered SQL files, a `schema_version` ledger, and a savepoint per
migration
([source](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/migrator.py#L62-L132)).
The idea transfers; its hand-written SQL splitter does not. It explicitly cannot parse every
legal SQL literal containing semicolons.

**Narrow recommendation.** Introduce a migration ledger with the next substantive schema—most
plausibly a dedicated Historian/search database—rather than converting every historical
migration now. Record version, stable name, checksum, and application time. Serialize concurrent
startup migration through SQLite transaction ownership, re-read the version after acquiring the
write lock, and retain WobbleBot's lost-race and forensic-data rules.

**Acceptance evidence if scheduled:**

- A fresh database and an upgraded current database converge on the same schema.
- Re-running at the current version is a no-op.
- Failure rolls back the current migration without claiming its version.
- Concurrent daemon startup produces one application and clean followers.
- An applied migration whose checksum changes fails loudly.
- The published v1.0-to-current upgrade-survivor gate remains green.

---

## Recommended future search architecture

### Separate canonical history from relevance search

Do not add generic memory/search methods to the already broad
[`StoragePort`](../../src/wobblebot/ports/storage.py), and do not add FTS/embedding tables to the
global schema applied by every writable SQLite adapter. Define two narrow capabilities when the
P4.6 gate opens:

- **`HistorianPort`** — canonical findings, source manifests, approval/supersession state, and
  exact keyed reads.
- **`EvidenceSearchPort`** — relevance search over a rebuildable projection, returning typed
  references to canonical rows.

This preserves the distinction between “what records exist?” and “which records may help answer
this question?” A relevance miss can never prove a trade, transfer, approval, or finding does not
exist.

### Suggested data shape

The exact schema belongs in the future P4.6 design, but the minimum information boundary is:

```text
historian_findings
  finding_id
  kind / scope_role / symbol
  finding_text
  status (active | superseded | revoked)
  model provider/name/revision
  prompt/schema/evaluator versions
  source_manifest_hash
  valid_from / valid_until
  approved_by / approved_at
  supersedes_id
  created_at

historian_sources
  finding_id
  source_database / source_table / source_row_id
  source_class
  content_hash
  observed_at

search_documents                 rebuildable projection
  document_id / canonical_ref
  source_class / role / symbol / timestamps
  title / body / content_hash
  indexed_at / projection_version

search_documents_fts             FTS5 index over the projection
```

Findings may be searchable for operator recall, but a Historian synthesis query must exclude
`source_class=finding` from primary evidence. This implements the release plan's rule that model
findings never recursively become evidence.

### FTS5 first

OpenChronicle's useful lexical patterns are:

- an external-content FTS5 table synchronized through insert/update/delete triggers;
- runtime feature detection;
- user text quoted/neutralized before `MATCH`;
- exact phrase mode distinct from any-token mode;
- structured filters before ranking and pagination; and
- deterministic tie-breaking.

See the pinned source for the [FTS table and
triggers](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/sqlite_store.py#L120-L151),
[runtime feature
detection](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/sqlite_store.py#L181-L221),
[filtered query construction and
ordering](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/sqlite_store.py#L837-L861),
and [query escaping plus phrase/any-token
handling](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/sqlite_store.py#L1023-L1043).

WobbleBot should begin with keyword/phrase search plus exact filters for source class, symbol,
role, time range, validity, and approval. The current web news route is a bounded pilot candidate:
it loads up to 1,000 recent rows, then performs a Python substring match against headlines and
coin tags ([`news.py`](../../src/wobblebot/web/routes/news.py)). Moving that predicate into a
storage/search query would remove the arbitrary pre-filter window without involving any LLM.

### Typed and bounded results

A future `SearchHit` should carry:

- canonical document reference;
- source table/row ID, content hash, source class, and observation time;
- a compact snippet with an explicit truncation field/name;
- retrieval channel (`keyword`, `semantic`, or `hybrid`);
- keyword rank, semantic similarity, and fusion score where applicable;
- effective filter set, index/projection version, and degradation state; and
- deterministic ordering keys.

`top_k` must be a total item budget, not “ranked results plus special records.” It is also not a
prompt budget. Callers need a separate hard character/token budget, deterministic drop order, and
a rule that current structured telemetry is never displaced by historical context.

OpenChronicle's `ScoredMemory` correctly warns that RRF is rank fusion, not calibrated confidence
([source](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/domain/models/scored_memory.py#L16-L41)).
WobbleBot must not threshold RRF as if it were outcome probability.

### Semantic retrieval only after a measured lexical miss

Before adding embeddings, create a WobbleBot-specific gold set of explicit questions and known
source rows. Measure at least recall@k, mean reciprocal rank, latency, prompt characters/tokens,
and whether citations identify the intended canonical row. Compare exact structured queries,
FTS5 keyword/phrase search, and only then semantic/hybrid candidates.

If embeddings earn their place, borrow these OpenChronicle invariants:

- provider, model, actual dimensions, model revision, settings fingerprint, and content hash are
  part of derived-artifact identity;
- a slow embedding result publishes only if the current document still matches its source hash;
- missing, stale, identity-mismatched, and permanently unembeddable rows are distinct health
  states;
- hybrid search may degrade to lexical results, but records the effective channel;
- an explicit semantic request fails visibly rather than silently changing treatment; and
- backfill progress and last success are observable.

Strengthen OpenChronicle's one-vector-per-memory design for WobbleBot. Use a composite
`(document_id, embedding_space_id)` identity, build a new generation beside the active one, and
switch the active generation atomically after coverage checks. A model change should not erase the
last working index before its replacement is ready.

Do not copy OpenChronicle's current full-table in-process vector scan. It is acceptable for a
small personal corpus, not a general WobbleBot retrieval architecture.

---

## LLM interaction boundary

Search can guide a model safely only when the consumer and authority boundary are explicit.

### Operator assistant

- Retrieve only **after** `AssistantPort.parse_intent` has produced a typed read-only recall or
  reporting request.
- Never insert durable memory into intent parsing. Historical content containing “approve,”
  “execute,” or an old command remains untrusted quoted data, not an instruction.
- An explicit recall query may return cited historical context or feed the existing free-form
  summarization path; it cannot construct or approve a `PendingCommand`.
- Raw conversation remains in `operator.db` under ADR-036 retention. Do not mirror turns into a
  store with no matching expiry semantics.

### Strategy advisor and Historian

- Do not silently inject generic memory into quant, risk, news, arbitrator, or Gremlin roles.
- Initial historical context should be deterministic structured features or canonical,
  provenance-complete findings—not free-form “important memories.”
- Any retrieval-augmented advisor work begins offline/shadow against identical persisted inputs
  and ADR-035 outcomes.
- Persist exact retrieved references/hashes, excerpts or projection version, filters, channel,
  degradation state, latency, prompt version, and model identity with each experimental output.
- If historical context materially drove a suggestion, that provenance must be structural. The
  suggestion remains excluded from auto-apply until an ADR and evidence explicitly admit it; the
  existing `news_materially_drove` flag does not cover this new source class.
- Retrieval failure falls back to today's no-history advisory input and records the degradation.
  Trading and Harvester behavior remain unchanged.

### Forbidden consumers

`GridEngine`, safety caps, command approval, `cli/live`, `cli/apply`, and `cli/harvest` do not query
a relevance index or OpenChronicle. A search result cannot become a completeness check, balance,
cost basis, approval, policy value, order, transfer, or execution instruction.

---

## Provenance, trust, retention, and prompt-injection requirements

OpenChronicle's `source` identifies how a record entered the service (`mcp`, `api`, manual), not
who asserted it or why it is trustworthy. OpenChronicle's own repository review says structured
provenance is required before automatic transcript, crawler, or tool-output ingestion and treats
caller-provided origin as an assertion rather than verified trust
([source](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/docs/design/0002-openclaw-memory-review.md#L349-L403)).

For WobbleBot:

- source identity is written by trusted code, outside model-authored prose;
- content hashes bind derived records to exact source state;
- source classes distinguish exchange/market, operator, external news, advisor-derived, and
  system material;
- eligibility is deterministic before LLM synthesis;
- approval, expiry, revocation, and supersession are explicit fields;
- a caller cannot self-label a write “trusted” or backdate the authoritative observation time;
- retained projections cannot outlive their canonical source contrary to ADR-036 without a
  separately ratified retention purpose; and
- credentials, withdrawal destinations, API keys, Discord tokens/identifiers, raw approvals, and
  unrestricted financial/operator text are never search documents.

Retrieved text is always delimited and labelled as untrusted historical evidence that cannot
override system instructions, current telemetry, policy, authorization, or safety limits.

---

## OpenChronicle patterns not to copy

| Pattern | Why it does not fit WobbleBot |
|---|---|
| Generic memory row as canonical evidence | Missing required provenance, versioning, approval, retention, and immutable audit semantics |
| Cross-project/global pinned records | Policy priority can crowd out relevance and blur scope; version-controlled prompts/config remain the source of standing rules |
| Automatic transcript/news/tool-output ingestion | Creates privacy, replay, retention, and persistent prompt-injection paths |
| In-place memory overwrite and hard delete | Financial/Historian evidence needs versioned supersession or revocation, not erased history |
| Project namespace as access control | Namespace is retrieval scope, not authorization |
| One shared read/write/delete API key | Violates least privilege and ADR-041 capability ownership |
| Full MCP surface exposed to a model | Gives the model write, pin, embed, delete, project-delete, and Git-onboard capabilities unrelated to safe recall |
| One shared SQLite connection behind a global `RLock` | Serializes unrelated reads/writes and discards WobbleBot's async/concurrent adapter discipline |
| Full-table in-memory vector ranking | Has an explicit corpus-size ceiling and unsuitable memory/latency behavior |
| Recent-row substring fallback when FTS is absent | A lossy degraded mode can produce incomplete or least-bad results while looking successful |
| Relevance results for exhaustive questions | Search ranking is not an audit enumeration or proof of absence |
| Enabling cloud embeddings without explicit opt-in and accounting | Sends full documents and queries off-host; requires explicit egress, cost, and provenance decisions. OpenChronicle itself defaults to no embedding provider. |

---

## Dogfood observation — ranking policy is part of the architecture

The existing OpenChronicle WobbleBot project held 65 memories, 32 pinned, at review time. A broad
hybrid query with `top_k=10` produced a roughly 22,000-token, truncated response dominated by
older pinned phase summaries. In the captured non-pinned run, a strict query returned zero exact
hits and a broader follow-up returned one marginal hit.

This is not proof that hybrid search is ineffective. It demonstrates that:

- pin/priority policy can dominate relevance;
- `top_k` does not bound prompt size;
- stale or superseded summaries need explicit lifecycle management;
- compact search hits should precede fetching full content; and
- retrieval quality depends on corpus curation and evaluation, not merely adding embeddings.

WobbleBot should not implement global pins. Standing safety rules remain version-controlled
prompts, configuration, and ADRs; search results must earn bounded context slots through explicit
scope and relevance.

---

## Suggested review and delivery sequence

### Slice A — ungated query correctness

- Add a symbol predicate to the advisor-suggestion storage query before `LIMIT`.
- Add adapter and service regression tests for wrong-symbol rows consuming the newest window.

This is a current defect correction, not Historian implementation.

### Slice B — SQLite artifact and maintenance hardening

- Stage, validate, and atomically publish SQLite backups.
- Stage and atomically publish CSV/GZip archives before deleting source rows.
- Make backup-before-vacuum an in-code dependency.
- Persist per-maintenance-job attempt/success/outcome state and expose it through current health
  surfaces.

These are independent hardening candidates and require explicit scheduling.

### Slice C — P4.6 design, only when its gate opens

- Ratify `HistorianPort` and `EvidenceSearchPort` boundaries.
- Define canonical findings/source manifests, retention, approval, and non-recursive evidence.
- Introduce a versioned migration ledger for the new store.
- Build deterministic projection plus FTS5 keyword/phrase search.
- Add cited, explicit operator recall only after typed intent classification.

### Slice D — measured semantic experiment

- Build and freeze the retrieval gold set.
- Benchmark FTS5 first.
- If lexical recall misses the agreed threshold, implement versioned shadow embeddings and hybrid
  fusion behind a default-off treatment.
- Evaluate quality, unsafe-recommendation rate, latency, token/cost impact, degradation, and
  provenance completeness before any consumer promotion.

No slice above is authorized merely because it appears in this assessment. Promotion requires the
roadmap/ADR path appropriate to its scope.

---

## Test boundaries for future work

### Current query fix

- Newest N suggestions belong to the wrong symbol; an older matching suggestion is returned.
- `limit` applies after eligibility.
- No-symbol, model, role, and time filters retain stable newest-first behavior.

### Artifact durability

- Fault injection before validation, during validation, and before rename.
- Invalid staged backups are never selected as latest and never cause good backup pruning.
- Archive failure never deletes source rows; retry needs no manual cleanup.
- Backup failure prevents `VACUUM`.

### Historian/search contract

- Scope/source/symbol/time/approval/expiry filters execute before rank and `top_k`.
- Stable deterministic pagination under tied ranks.
- Total result and prompt budgets are independently enforced.
- Compact fields are explicitly named as snippets/truncated values.
- Every hit carries source row IDs, hashes, observation times, and effective retrieval channel.
- No finding can become primary evidence for another finding.
- Keyword/hybrid degradation is labelled; explicit semantic mode fails visibly.
- An edited source refuses stale embedding publication.
- A new embedding generation does not replace the active generation until coverage/integrity pass.

### Authority and deployment

- Retrieved text containing command/approval language cannot bypass `awaiting_confirmation`.
- No search result can originate `ExecuteProposalCommand` or any transfer path.
- A future indexer/sidecar has zero Kraken, Harvester, trader, Discord, web-session, or cloud-LLM
  credentials unless a separately traced operation requires one.
- It receives no broad read-write `/app/data` mount.
- Disabled, unavailable, timeout, and degraded-search cases preserve current trading behavior.
- Cloud embedding calls, if ever admitted, use a separately ratified ADR-014-compatible cost and
  egress path, reusing or amending existing machinery only if that design approves it.

---

## Decision triggers and open questions

| Question | Trigger / evidence required |
|---|---|
| Should the current news UI gain FTS5? | Corpus exceeds the capped substring window or operator search use demonstrates missed/slow results |
| Should the migration ledger be generalized to every existing DB? | A new cross-version migration or audit need shows the current idempotent functions are operationally insufficient |
| Should operator recall search raw conversation turns? | Explicit operator use case plus retention/privacy design; automatic injection remains declined |
| Should OpenChronicle be a runtime search adapter? | A constrained read-only workflow beats WobbleBot-owned FTS in evaluation and passes capability/security review |
| Should embeddings be added? | Frozen gold set shows material FTS5 recall failure and an embedding candidate clears quality, latency, egress, and cost gates |
| Should retrieved history influence the advisor? | Offline treatment improves ADR-035 outcomes without increasing unsafe recommendations and receives a new provenance/auto-apply decision |
| Should a search index move beyond SQLite? | Measured corpus, latency, or concurrency pressure exceeds the SQLite design; not hypothetical scale |

---

## Maturity and licensing notes

OpenChronicle v3.3.0 is young but carefully engineered: hexagonal boundaries, Windows/Linux CI,
strong search invariants, documented degradation paths, and robust backup/migration tests. Its
API stability policy began with v3.0.0 on 2026-08-28, the project describes itself as single-user,
and current scale is one SQLite connection plus in-process vector ranking. It should not be
treated as financial infrastructure merely because its local test suite is substantial.

OpenChronicle requires Python 3.14+. Its
[`pyproject.toml`](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/pyproject.toml)
declares `AGPL-3.0-only`, while its
[`README`](https://github.com/CarlDog/openchronicle-mcp/tree/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45#license)
describes AGPL v3 “or later.” WobbleBot is MIT and Python 3.13+. Treat that upstream license
wording as an inconsistency to resolve, not permission to choose the more convenient reading.
Architectural ideas and invariants should be implemented independently in WobbleBot; do not copy
OpenChronicle source, comments, tests, or schema text without accepting and reviewing the
applicable license obligations. Any later process-separated sidecar proposal still needs legal
and deployment review. This paragraph is engineering guidance, not legal advice.

---

## Primary OpenChronicle references

- [Repository and README](https://github.com/CarlDog/openchronicle-mcp/tree/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45)
- [Architecture](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/docs/architecture/ARCHITECTURE.md)
- [Security posture](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/docs/configuration/security_posture.md)
- [API stability policy](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/docs/api/STABILITY.md)
- [Versioned migration runner](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/migrator.py)
- [Atomic online backup](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/backup.py)
- [SQLite store and FTS5](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/infrastructure/persistence/sqlite_store.py)
- [Hybrid/semantic retrieval](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/application/services/embedding_service.py)
- [Scored search result](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/src/openchronicle/core/domain/models/scored_memory.py)
- [OpenClaw memory-pattern review](https://github.com/CarlDog/openchronicle-mcp/blob/7349f94ab8bd8b9a8c60e1def63ad4997f7f9a45/docs/design/0002-openclaw-memory-review.md)
