# Notices, attributions, and trademark statements

This project includes nominative references to several third-party
products and services. These references are used solely to describe
which platforms WobbleBot integrates with or depends on. **No
affiliation, sponsorship, or endorsement is claimed or implied.**

## Affiliation disclaimers

WobbleBot is **not affiliated with, endorsed by, or sponsored by**
any of the following:

- **Kraken** — registered trademark of Payward, Inc. WobbleBot uses
  Kraken's publicly-documented REST API as a customer integration
  under Kraken's published API Terms of Service. The maintainer holds
  a personal Kraken account and integrates against it; nothing in this
  project should be read as a partnership with or endorsement by
  Kraken.
- **Discord** — trademark of Discord Inc. WobbleBot uses Discord's
  Bot API via the `discord.py` library as one transport for the
  operator-interaction layer. No Discord affiliation is claimed.
- **Anthropic / Claude** — Anthropic, PBC and its products. The
  `AnthropicAdapter` calls Anthropic's public Messages API as an
  optional LLM provider. No affiliation.
- **OpenAI / ChatGPT / GPT** — OpenAI, OpCo, LLC and its products.
  The `OpenAIAdapter` calls OpenAI's public Chat Completions API as
  an optional LLM provider. The WobbleBot brand marks in
  `src/wobblebot/web/static/wobblebot*.png` were generated via
  ChatGPT in May 2026; per OpenAI's Terms of Use at time of
  generation, the operator holds usage rights to the generated
  content. No affiliation with OpenAI is claimed.
- **Google / Gemini** — Google LLC and its products. The
  `GoogleAdapter` calls Google's public Generative Language API as
  an optional LLM provider. No affiliation.
- **Ollama** — open-source project hosted at https://ollama.com.
  WobbleBot's `OllamaAdapter` calls Ollama's local API. No
  affiliation.

## Bundled assets

This repository bundles two static image assets sourced from
third-party services for navigational convenience. Each is shipped
locally (not loaded via remote `<img src>`) so dashboard rendering
makes zero per-page external requests.

- **`src/wobblebot/web/static/kraken-icon.png`** — Kraken Pro
  favicon, downloaded from `https://pro.kraken.com/favicon.ico` on
  2026-05-22, converted from 48×48 ICO to PNG. Used as a navigation
  icon in the operator's user-menu dropdown link to the Kraken Pro
  account home. The Kraken logo is a registered trademark of
  Payward, Inc.; bundling is for fair-use navigational reference
  only and may need to be replaced with text-only navigation in
  redistributed forks. Forks intended for wide distribution should
  either obtain explicit permission from Payward or remove this
  asset.

- **`src/wobblebot/web/static/carldog-avatar.png`** — the
  maintainer's personal avatar (CarlDog mascot). Used in the
  navbar user-menu trigger as identity affordance. The avatar is
  the maintainer's own image. Forks should replace it with their
  own.

- **Lucide icon path data (inline SVG in `news.html`).** The
  ``external-link`` icon (rendered next to news headlines when a
  source URL is present) uses the path data from
  [Lucide](https://lucide.dev), an MIT-licensed icon set. The SVG
  is inlined in the template rather than bundled as a file —
  attribution preserved here per MIT license terms. If more icons
  are added in the future, consider shipping a single
  ``static/icons.svg`` sprite with one shared attribution block.

## WobbleBot brand mark

The WobbleBot squircle icon (`wobblebot-icon-{256,512,1024}.png`),
brand mark (`wobblebot.png`), favicon (`favicon.png`), and login
hero (`wobblebot-hero.png`) were generated via ChatGPT (OpenAI's
image generation) in May 2026. Per OpenAI's Terms of Use at the
time of generation, the user of the generation service holds
usage rights to the output. The maintainer (CarlDog) licenses
these assets to forks under the same MIT terms as the rest of
the repository — see [`LICENSE`](LICENSE).

## Dependencies

WobbleBot's runtime dependencies (httpx, fastapi, uvicorn, jinja2,
bcrypt, itsdangerous, python-multipart, ruamel.yaml, python-dotenv,
python-frontmatter, aiosqlite, feedparser, discord.py, rapidfuzz,
tzdata, pydantic) are pulled via `pip` and used per their own
licenses (predominantly MIT and BSD-3-Clause; all compatible with
WobbleBot's MIT license). See `pyproject.toml` for the canonical
list. No third-party source code is vendored into this repository;
every dependency is fetched at install time from PyPI.

## Trademark policy

Trademarked names mentioned anywhere in this repository (Kraken,
Discord, Anthropic, OpenAI, Google, Gemini, Ollama, Bitcoin, BTC,
ETH, etc.) are used in their nominative sense to describe
integration points, supported services, or asset symbols. All
trademarks are the property of their respective owners. If you
are the trademark holder of any name mentioned here and have
concerns about a specific reference, please open a GitHub issue;
the maintainer will respond promptly.

## Reporting concerns

For trademark or attribution concerns, open a GitHub issue. For
security vulnerabilities, see [`SECURITY.md`](SECURITY.md) instead.
