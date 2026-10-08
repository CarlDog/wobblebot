# Post-merge CodeQL alerts 48 and 49

Inspected baseline: `dfd1156ee3d854e114898814823a1b40385c2a98`, the PR #170
merge. These alerts were first reported by main analysis `37782370490`, using
query source `a7057b836bf29767461026718b14d20790b9085f`. Parent-provided
authenticated GitHub details were checked against the exact integrated source.
Current verification belongs in [the roadmap](../planning/roadmap.md).
Earlier decisions about alerts 44–46 do not dispose of these new alerts.

## Alert 48: path-injection warning at the existence check

[Alert 48](https://github.com/CarlDog/wobblebot/security/code-scanning/48)
models both health routes' `config: WebConfig = Depends(get_config)` as a
request source. The reported flow continues through `config.operator_db`,
`_path_or_none`, `fetch_daemon_freshness`, and `_heartbeats_or_empty` to
`operator_db.exists()`. As with the earlier URI-sink assessment, the actual
dependency returns startup `request.app.state.config`; request parameters do
not select these paths. Independent review found no remote trust-boundary
crossing in the reported flows. This is not a blanket claim about other paths.

There is a demonstrated adjacent reliability defect: the existence check and
URI construction precede the error handler. On the supported Python 3.13
runtime, an overlong filename raised `OSError` (errno 36) instead of degrading
health to unknown. A denied lookup can fail similarly.

The fix removes the redundant existence check and puts URI construction inside
the existing handler. SQLite's `mode=ro` open is the authoritative read attempt:
missing/unreachable databases still produce unknown and cannot be created by
the reader. Arbitrary operator-configured paths, filename URI escaping and WAL
read semantics remain supported. No directory allowlist, sanitizer or scanner
suppression was added. Removing this sink is not proof that path injection was
exploitable or that every future scanner result is resolved.

## Alert 49: configuration name in the missing-secret diagnostic

[Alert 49](https://github.com/CarlDog/wobblebot/security/code-scanning/49)
tracks `web_config.session_secret_env_var` into `env_var` and then a logger
format argument. That field is the environment-variable **name**, not the
secret value returned by `os.environ.get`. The log branch executes only when
the looked-up value is absent or empty; no actual cookie-signing-key disclosure
was established.

The diagnostic now uses static instructions referring to
`web.session_secret_env_var`, retaining the mint command, custom-variable
support and startup refusal. This avoids echoing arbitrary configured text,
including a key accidentally pasted into the name field or control characters.
Tests cover unset, empty and populated custom variables and assert that neither
the configured name nor the synthetic secret value enters the log.

## Verification boundaries

The exact-baseline negative control fails four new cases (two missing-secret
diagnostics and two filesystem-failure cases); successful lookup and missing-file
non-creation remain positive controls. Restoring both implementations passes all
six. Endpoint tests retain adversarial query-parameter coverage. Full quality,
test and independent-review receipts belong in the roadmap. Fresh PR/main
CodeQL must establish scanner disposition; no alerts were dismissed, no security
configuration changed, and no deployment or production acceptance is claimed.
