# Web UI static assets

Files here ship to the operator via FastAPI's `StaticFiles` mount at
`/static/...`:

- **`base.css`** — minimal dashboard styles. Committed; edit freely
  per operator branding (the file lives in your repo, not behind
  the reverse proxy).
- **`htmx.min.js`** — HTMX 2.x for partial-update polling on the
  cost ledger + open-orders cards. **Currently a placeholder** —
  vendor a real HTMX build before deploying:

  ```bash
  curl -L --output htmx.min.js \
    https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
  ```

  Pin to 2.x. HTMX is stable but minor versions occasionally
  tweak default attribute behavior; verify SHA-256 from
  <https://htmx.org/> before swapping in.
- **`theme-init.js` / `nav.js` / `notifications-seen.js`** — v1.1:
  extracted from inline `<script>` blocks in `base.html` /
  `layout.html` / `notifications.html` so the dashboard's
  Content-Security-Policy (`web/middleware.py`) can set
  `script-src 'self'` with no `'unsafe-inline'`. No Jinja
  interpolation lives in any of these — they're plain JS, editable
  like any other static file. Adding a NEW inline `<script>` to a
  template will silently fail to execute under the CSP (blocked, not
  errored) — put new script logic in a file here instead.
  `tests/web/test_security_headers.py::TestNoInlineScriptsRemain`
  guards against a template regrowing an inline script.

Without HTMX the dashboard chrome (nav, login, mutation confirm
flow) all work — only the polled cards stay static until a full
page reload.

A Stage 7.5 follow-on test will verify `htmx.min.js` contains the
HTMX module signature (so a forgotten vendor step fails CI rather
than reaching production silently).
