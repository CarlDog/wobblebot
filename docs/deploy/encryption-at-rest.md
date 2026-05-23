# Encryption-at-rest deployment guide

Companion to [`reverse-proxy.md`](reverse-proxy.md). That doc
covers traffic-on-the-wire security (HTTPS); this one covers
data-on-disk security.

WobbleBot's SQLite databases under `data/` hold financial data
(orders, trades, withdrawal destinations, operator chat with the
LLM, password hashes, LLM cost ledger). v1.0 stores all of it as
plaintext SQLite files — encryption-at-rest is **the operating
system's job**, not the app's, per the same architectural
principle that puts HTTPS at the reverse-proxy layer.

## Threat models worth distinguishing

Encryption-at-rest defends against some threats but is irrelevant
to others. Be honest about which one you're addressing:

| Threat | At-rest encryption helps? |
|---|---|
| **Disk theft** (laptop stolen, NAS drive pulled) | ✅ Yes — the literal scenario it's designed for |
| **Cold backups leak** (Synology backup chain copied to cloud / shared storage) | ✅ Yes — if backups are also encrypted |
| **Live read while bot is running** (process or memory access by an attacker who's on the host) | ❌ No — the running process holds the key, so anything that reads its memory or its open DB connections bypasses encryption |
| **Memory dump** (sufficient host compromise) | ❌ No — same reason |
| **Insider with operator's credentials** | ❌ No — that's an authentication problem, not encryption |

If your only realistic threat is the live-read or memory-dump
case (rare for a single-operator local deployment), at-rest
encryption is theater. If disk theft or backup leakage is on the
table, it's load-bearing.

## Three tiers ranked by effort + threat coverage

### Tier 0 — OS-level disk encryption (recommended for everyone)

**What:** BitLocker (Windows), FileVault (macOS), LUKS (Linux),
or Synology's volume-encryption option. Encrypts the entire
filesystem; the OS unlocks it at boot via a passphrase or TPM-
backed key. WobbleBot sees an ordinary filesystem.

**What it covers:** disk theft, cold-backup leakage (if backups
also live on encrypted storage — most NAS / cloud backup tools
respect this when configured).

**Effort:** zero WobbleBot code change. Operating-system feature.

**Recommended for:** every WobbleBot deployment. If you're on
Windows and not running BitLocker — that's the single highest-
leverage security improvement available to your install. Same
for Synology (Volume Encryption in DSM Storage Manager).

### Tier 1 — SQLCipher (v1.1 candidate, real work)

**What:** drop-in SQLite replacement that transparently encrypts
SQLite database files with AES-256. App passes the passphrase on
connection open; reads/writes are encrypted in-flight to disk.

**What it covers everything Tier 0 covers PLUS:** the case where
a cold backup file gets pulled out from under the OS encryption
boundary (copied to a USB drive without re-encrypting; emailed;
shared with another machine without OS-level disk encryption).
Each `.db` file is its own self-contained encrypted blob — the
key has to follow the file.

**Effort:** ~1-2 days work. Swap `aiosqlite` for an SQLCipher-
aware binding (`pysqlcipher3-aiosqlite` or similar; needs
investigation), schema migration to encrypted format,
performance check (SQLCipher adds ~5-15% overhead vs vanilla
SQLite for most workloads), operator key-management decision
(use the existing `WOBBLEBOT_WEB_SESSION_SECRET`? new env var?
file-based?), one new runtime dep.

**Recommended for:** deployments where backup files cross trust
boundaries (cloud backup, shared NAS with non-encrypted volumes,
backup-to-S3 scenarios). v1.1 candidate.

### Tier 2 — Per-field encryption (probably overkill)

**What:** encrypt specific sensitive columns (e.g.,
`conversation_turns.body`, `transfer_proposals.destination_label`)
inline in the application. Most surgical; most complex.

**What it covers:** the specific case where you want SOME data
encrypted but other data queryable in plaintext (analytics, audit
trail review). For WobbleBot's threat profile, this is overkill
— Tier 0 + 1 cover the realistic scenarios.

**Recommended for:** essentially never, for this app. Documented
for completeness.

## Verification: what's actually in the DBs

| DB | Sensitive content |
|---|---|
| `operator.db` | Password hashes (already bcrypt), session secret (env-only), conversation turns with LLM, LLM cost ledger, pending commands |
| `live.db` | Orders, trades, balance snapshots, grid state |
| `harvest.db` | Withdrawal destinations + amounts (real bank affordances) |
| `observe.db` | Price snapshots — **public market data, non-sensitive** |
| `news.db` | News items — **public, non-sensitive** |
| `advise.db` | Advisor suggestions — plaintext but low sensitivity |

`observe.db` and `news.db` could be unencrypted with no privacy
loss; the other four hold the load-bearing financial + auth data.

## File permissions (Tier 0 companion)

Encryption defends content; permissions defend access. Even on an
encrypted disk, an unprivileged user account on the same machine
can read `data/*.db` if the files are world-readable. The
discipline is identical across OS:

- **`.env`** → mode `0600` (owner read/write only). Holds the
  Kraken API keys + the Discord bot token + `WOBBLEBOT_WEB_SESSION_SECRET`.
  Any non-operator user on the host who reads this owns the bot.
- **`data/*.db`** → mode `0600`. Holds the financial data tier
  (orders, trades, withdrawal destinations, password hashes).
- **`data/backups/*.db`** + **`data/archive/*.csv`** → mode `0600`.
  Inherits sensitivity from the source DBs.
- **`config/settings.yml`** → mode `0644` acceptable (no secrets
  inside; secrets live in `.env`). The schema-drift discipline
  keeps secrets out of the YAML by construction.

### Setting permissions

```bash
# Linux / macOS — single command, idempotent
chmod 600 .env data/*.db data/backups/*.db data/archive/*.csv 2>/dev/null
```

```powershell
# Windows — use icacls to remove inheritance + restrict to owner
icacls .env /inheritance:r /grant:r "$env:USERNAME`:F"
Get-ChildItem data\*.db -Recurse | ForEach-Object {
    icacls $_.FullName /inheritance:r /grant:r "$env:USERNAME`:F"
}
```

These settings stay sticky as long as the files aren't recreated
by a process running under a different account. `cli/maintenance`
respects the owner's existing permissions when it writes backup
files (Python's `open()` inherits the umask).

## What v1.0 ships

- **Tier 0 (operator-managed):** documentation telling you to
  enable OS-level disk encryption. No code change.
- **Tier 1 (deferred):** v1.1 candidate documented in
  [`v1.1/infrastructure.md`](../release/v1.1/infrastructure.md).
- **Tier 2:** not planned.

If your install plan involves cloud deployment, shared NAS
volumes without encryption, or backup destinations outside your
direct trust boundary, plan to either (a) wait for the SQLCipher
v1.1 work, or (b) encrypt backups with `gpg` / `age` before they
leave your trust boundary.
