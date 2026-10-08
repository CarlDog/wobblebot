# PR #170 CodeQL triage

This records the evidence behind the proposed handling of the five alerts on
`bcc29e0ada9970ad8a6b4c34b87d9c4e97651856`. Current verification and acceptance
belong in the [roadmap](../planning/roadmap.md#cloud-product-completion-verification).
An analysis job succeeding does not mean its aggregate security check passed.
The original aggregate reported one critical and four high findings. No alert
was dismissed and no analysis configuration, severity or suppression was changed.

## HTTP probe: alert 43, `py/full-ssrf` (critical)

The reported flow is real: `WOBBLEBOT_HEALTH_URL` in `tools/healthcheck.py`
`main` flows through `_check_http` to `urllib.request.urlopen`. The input is a
container/operator setting, not an HTTP request; no remote exploit was observed.
Nevertheless, unrestricted destinations, URL schemes, redirects and environment
proxies exceed this tool's documented purpose: probing its own web `/healthz`.

The implementation now permits HTTP `/healthz` only, with `localhost`,
`127.0.0.1` or `[::1]` and a configurable valid port. It refuses credentials,
query/fragment delimiters, control characters and invalid timeouts before I/O.
The request uses a fixed loopback address and constant path through HTTPConnection;
no DNS lookup of an input host, proxy configuration or redirect following occurs.
Response and connection resources are closed on success and failure. Invalid
input does not echo credential-bearing URLs into the Docker health log.

Regression coverage uses rejected-target transport mocks, a real local redirect
server, proxy environment controls, valid local endpoints and resource cleanup.
The former implementation fails the fifteen rejection/redirect cases in a
separate disposable checkout. This is a bounded local reproduction, not an
attempt against a private or production target.

## Configured database paths: alerts 44–46, `py/path-injection` (high)

**Recommendation for authorized review: false positives for these individual
flows.** Do not infer a blanket exception for database paths or future inputs.
Keep `resolve().as_uri()` so spaces, percent signs, fragments and question marks
are encoded as filename content rather than SQLite URI options. A blanket
repository-directory allowlist would break supported operator-selected databases.

- **44 and 45:** CodeQL models the `config: WebConfig = Depends(get_config)`
  parameters of `web/routes/health.py`'s `health_page` and `health_overall_json`
  as sources. `get_config` in `web/dependencies.py` instead returns
  `request.app.state.config`; `create_app` in `web/app.py` initializes that state
  from startup configuration supplied by `cli/web.py`. The health routes consume
  its observe/advise/operator paths through `load_health_snapshot`,
  `fetch_daemon_freshness`, `_read_daemon`/`_latest_iso_timestamp` and
  `_heartbeats_or_empty`. They do not deserialize WebConfig from query/body data.
  The web settings route changes display preferences, not database configuration.
- **46:** modeled sources are CLI `parse_args` values in `tools/auditor.py`,
  `import_kraken_history.py`, `reconcile_trade_history.py` and `run_advisor.py`.
  Their `--db`/`--db-path` options deliberately select operator-owned files within
  process permissions. The reconciliation reader uses `read_only=True`; the other
  listed construction paths use the default writable adapter and do not take the
  highlighted read-only branch. No crossing from an untrusted remote principal
  to filesystem authority was found in these reported flows.

Two actual FastAPI endpoint tests submit adversarial database query parameters
and assert that only startup-configured paths reach the freshness consumer. Both
fail when a disposable mutation makes `get_config` honor those query parameters,
and pass with the original dependency restored. Existing read-only refusal,
missing-file, concurrent-writer and escaped-filename tests remain binding.
An independent source review agrees with this narrow recommendation. It is not
an alert dismissal, accepted exception, or assertion that future path inputs are safe.

## Image reference: alert 47, `py/polynomial-redos` (high)

The source is the generator's CLI `--image`, passed to a full-match regex. The
generator writes a reviewable YAML plan; it does not run Docker or a shell.
The original pattern has one unbounded repository character class excluding its
`@` delimiter and a fixed-length digest. A bounded local measurement of repeated
hyphens (10,000 / 100,000 / 1,000,000) did not reproduce superlinear growth.
This is not evidence of an exploited denial of service.

The regex has nevertheless been replaced with straightforward linear-time string
parsing: nonempty repository in the same ASCII character set, `@sha256:` delimiter,
and exactly 64 lowercase hexadecimal characters. Ports, tags and punctuation
retain their original acceptance. Malformed references fail before config reads
or output creation. Targeted cases and 3,000 deterministic varied comparisons
against the old pattern preserve the accepted syntax. This removes the regex
hazard without a scanner suppression or a new dependency.

Original alert links: [43](https://github.com/CarlDog/wobblebot/security/code-scanning/43),
[44](https://github.com/CarlDog/wobblebot/security/code-scanning/44),
[45](https://github.com/CarlDog/wobblebot/security/code-scanning/45),
[46](https://github.com/CarlDog/wobblebot/security/code-scanning/46),
[47](https://github.com/CarlDog/wobblebot/security/code-scanning/47).
Exact flows were supplied from the authenticated GitHub UI because this session's
connector rejects the code-scanning details endpoint. Runtime code and reachable
call sites were independently inspected in the checkout.
