# Web UI static assets

Three files live here and ship to the operator via FastAPI's
`StaticFiles` mount at `/static/...`:

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

Without HTMX the dashboard chrome (nav, login, mutation confirm
flow) all work — only the polled cards stay static until a full
page reload.

A Stage 7.5 follow-on test will verify `htmx.min.js` contains the
HTMX module signature (so a forgotten vendor step fails CI rather
than reaching production silently).
