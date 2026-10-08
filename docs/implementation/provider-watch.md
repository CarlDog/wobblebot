# Provider maintenance ownership and manual watch ledger

N5 separates public contract/pricing drift from model selection. The project role
**provider-integrator** owns contract and pricing review; **model-review-owner**
owns model-catalog changes and reconciliation against
[advisor seats](../reference/advisor-seats.md), including Atlas and every primary
and fallback seat. These are work-item owners, not claims that an external person
accepted an unattended rota. GH97 and GH22 remain separate, linked obligations;
no issue was closed or remotely modified.

`tools/provider_watch.py` consumes reviewed public-source fingerprints into a
separate local SQLite ledger. It does not fetch sources, use credentials, install
schedules, send notifications, reseat models or edit prices. First observations
form baselines; changed digests produce schema-versioned review events. Repeating
an observation or the same before/after transition does not produce another event.
An older or contradictory snapshot fails atomically. Missing provider/kind coverage
is listed explicitly, including the six LLM providers and Kraken's contract lane.
Each seat must also be checked against the register by the model-review owner;
provider coverage alone is not seat qualification.

```bash
python tools/provider_watch.py --snapshot /path/reviewed-public-snapshot.json --database data/provider-watch.db
```

Input is a JSON list of records with **only** `provider`, `kind` (`contract`,
`pricing`, `models`), credential-free HTTPS `source_url`, timezone-aware
`observed_at`, and lowercase SHA-256 `sha256`. Hash a bounded, consistently
normalized public contract/pricing/catalog excerpt; retain source/date and review
notes with the work item. Never hash account pages or copy API keys into inputs.
Snapshots are bounded to 1 MiB / 1,000 records. The database parent must already
exist. Output contains changes, assigned role and missing coverage; exit 0 means
no changes/gaps, 1 means review needed, 2 means invalid input/storage failure.
A fingerprint detects content drift, not semantic breakage: review the official
source and run the affected adapter contracts before changing behavior.

No unattended interval, delivery destination or stop policy has been accepted.
Consequently there is no scheduled watcher claiming ongoing coverage. To activate
one, name the responsible operator, accepted interval/stop rule and destination,
then verify source access and notification delivery. Actual public/API snapshots
and private account contracts remain separate evidence from synthetic change tests.

## Legacy funding retention decision

Retain the current Harvester-only legacy `/0/private/Withdraw` implementation for
this candidate. The [official legacy reference](https://docs.kraken.com/api-reference/funding/withdraw-funds)
reviewed 2026-10-03 still documents an active but deprecated endpoint, with the
existing asset/key/amount request and refid response. Retention is deliberate,
not a claim of permanent support or fresh authenticated verification.

The [Funding Beta withdrawal contract](https://docs.kraken.com/api-reference/funding-beta/create-funding-withdrawal)
uses a different path, method/address identifiers, nested amounts/fees, and
withdrawal identity. It is not a path-only substitution. Before migration, verify
actual account/method/address mappings, nonce/signing rules, fee limits, status
reconciliation and ambiguous-outcome handling. Preserve Harvester-only keys,
immutable approvals, withdrawal limits and no-blind-replay claims. Never try the
other endpoint after an uncertain withdrawal: that could duplicate the effect.
A removal/contract-break notice triggers an explicit migration work item; until
verified, affected withdrawals stop for operator action. No beta transfer, new
permission, persistent-data migration or real withdrawal was attempted.
