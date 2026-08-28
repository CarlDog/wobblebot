# Manual reference fixtures

These files are inert, human-run evaluation inputs. They are not production configuration, test
suite fixtures, account receipts, or authoritative examples of current market/account state.

## Gremlin directional-forecast prompt

`gremlin-directional-forecast-prompt-2026-08-23.txt` is an operator-authored, complete prompt saved
on 2026-08-23 to exercise the Gremlin's falsifiable directional schema against a realistic-shaped
metrics payload. The values are committed only as test data; they are not verified production
facts and must not be copied into status, P&L, or exposure records. The file contains no credential
fields.

Manual Ollama reproduction from the repository root:

```powershell
Get-Content -Raw docs/reference/fixtures/gremlin-directional-forecast-prompt-2026-08-23.txt |
  ollama run qwen2.5:3b-instruct-q4_K_M
```

Because the role is intentionally stochastic, the direction itself is not pinned. A usable result
is one strict JSON object with `role: "gremlin"`, `confidence: "high"` or `"low"`, and a
`recommendations` object containing only `direction` (`up`, `down`, or `chop`) and numeric
`horizon_hours` from 4 through 72. Do not add live secrets or replace the payload with an expanded
production environment/config dump.
