# Help Directory / FAQ / Dictionary — design proposal

**Status: proposed, filed for later review 2026-08-21.** Not committed to any
phase of `roadmap.md`. Motivated directly by the 2026-08-20 mass book-vanish
incident: the "Book vanished: X/USD — trading held" notification read as
alarming regardless of cause (Kraken's own dead-man's-switch firing safely
during a self-resolving API outage vs. a genuinely unexplained external
cancel), and the operator had no in-app place to go ask "what does this
actually mean." That gap generalizes: the app has grown real conceptual
surface area (grid spacing, offside parking, cost-basis guards, re-anchor
economics, the MoE panel, the Chaos Gremlin, the dead-man's-switch, book-vanish
holds, ADR-002's advisory-only firewall) with no single place explaining any
of it to the person actually running the bot.

## Goals

- A **searchable, well-organized, easy-to-navigate** help surface reachable
  from the web dashboard's user dropdown ("Help").
- The **same content** reachable from Discord and the CLI — one source, three
  renderers, not three separately-maintained copies.
- A foundation that notifications/alerts can **deep-link into** ("Book
  vanished" → "Learn more" → the exact explainer), so the next confusing
  alert has somewhere to send the operator instead of just sounding scary.

## What already exists — extend it, don't duplicate it

`services/operator_service.py` already has `_HELP_ENTRIES`: a tuple of
`HelpEntry(kind, category, description)` that powers Discord's `help` intent
today (ask the bot what it can do, it lists commands/queries). It's a
**command reference**, not a concept/FAQ dictionary, and it's kept in sync
with `operator.md`'s catalog + the intent union via an existing SSOT drift
test (`tests/config/test_operator_catalog_ssot.py`).

This proposal treats that as the `category="command"` slice of a **larger**
content set, not something to replace. The command catalog stays
code-generated (it must — it's derived from the actual intent union so it
can never drift from what the bot really supports). The new content
(concepts, FAQ, safety/architecture explainers) is **author-written**, since
there's no code to generate "what does offside mean" from.

## Content model

**File-based, mirroring the existing `config/prompts/*.md` convention**
(YAML frontmatter + markdown body, loaded via a small parser — the project
already has this exact pattern working for prompts via
`wobblebot.config.prompts`). Proposed location: `config/help/*.md` — the
closer analog to prompts (shipped-and-loaded-at-runtime content) than
`docs/` (repo-only reference material the running app never reads).
**Open question:** confirm `config/help/` vs. a new top-level `help/`
directory before implementing — `config/` today is entirely
operator-tunable settings + prompts; help content is neither, so it may
deserve its own home. Low-stakes either way.

Each file:

```yaml
---
slug: book-vanished
category: concept        # concept | faq | safety
title: "Book vanished — what it means"
tags: [dead-mans-switch, adr-037, holds]
---

A symbol's open orders disappeared from Kraken without the engine
cancelling them itself. Two real causes:

1. **Kraken's own dead-man's-switch fired** (ADR-021) — if the bot can't
   reach Kraken for ~60s, Kraken auto-cancels everything as a safety net.
   Self-resolving; nothing to fix.
2. **A genuinely external cancel** — something else touched the account.
   Worth investigating.

The engine can't always tell which happened, so it holds the symbol
either way and waits for you to resume it.
```

A thin loader module (`services/help_content.py`, parallel to
`config/prompts.py`) parses these into structured entries — slug, category,
title, tags, short excerpt (first paragraph, for listings/search results),
full body — and merges them with the code-generated `command` entries from
`_HELP_ENTRIES` into one `HelpEntry`-shaped sequence every surface reads
from. One data source; the three sections below are just three ways of
looking at it.

## Web: `/help`

- New route + template. Nav placement: a new item in `layout.html`'s
  `user-menu-dropdown` (between Settings and Kraken Pro, matching the
  existing label-left/icon-right item pattern), labeled **Help**.
- Layout: category sidebar (Concepts / FAQ / Safety & Design / Commands) +
  content pane. **Open question:** given the likely content volume (dozens
  of entries, not hundreds), a simpler single-page-with-sticky-TOC might be
  just as navigable and gets browser `Ctrl+F` for free — worth a quick
  prototype of both before committing, rather than assuming the
  docs-site-sidebar pattern is automatically better here.
- Search: **client-side**, no backend search infra — content volume doesn't
  warrant it. A small JSON index (slug/title/category/tags/excerpt) embedded
  or fetched once; start with hand-rolled substring/keyword matching
  (matches the project's existing "no remote fetch, self-contained assets,
  minimal hand-rolled JS" posture — see `nav.js`/`modal.js`), upgrade to a
  locally-vendored fuzzy-match library only if plain substring matching
  proves too weak in practice. No over-engineering the search box for a
  few dozen entries.
- Deep-linking: `/help#<slug>` (or `?topic=<slug>`) so any page or
  notification can link straight to one entry. The concrete first use:
  the "Book vanished" notification message gains a link to
  `/help#book-vanished`. Retrofitting other existing notifications with
  deep-links is natural, cheap follow-up once the page exists — doesn't
  need to block shipping it.

## Discord

Extend the existing intent-routing (the `operator.md` catalog +
`operator_service.py`'s query handling) with a new query kind — natural-
language "what does X mean" / "explain X" — that searches `concept`/`faq`/
`safety` entries by slug/title/tags and returns the matching entry (or a
short "did you mean: A, B, C" list when ambiguous). This is a **near-free
extension**, not new infrastructure: the bot already maps free-text onto a
catalog for every other query kind (per `operator.md`'s own routing rules —
"the bot renders the catalog from code; do not enumerate it yourself"), so
this is one more catalog member, not a new mechanism.

Response length: Discord embeds have practical limits, so long entry bodies
should truncate with a "read the full explanation: `/help#<slug>`" pointer
back to the web page — mirroring how other Discord responses in this
project already stay concise and point to the dashboard for depth.

## CLI — the open decision

The other 15 `cli/` entry points are daemons or one-shot diagnostics; unlike
the web dashboard or Discord, nobody's expected to sit at a terminal asking
"what's cost basis" as a daily workflow. Two options, genuinely undecided:

- **Option A (recommended to start): a one-shot lookup**, likely
  `tools/help.py` (matching the character of `tools/first_real_trade.py` /
  `tools/run_cloud_check.py` — one-shot diagnostics, not daemons) —
  `--list [category]`, `--search <term>`, `--show <slug>`, prints to
  stdout. Small, no new dependencies, done in an afternoon.
- **Option B: a full interactive terminal browser** (search-as-you-type,
  category navigation). Meaningfully bigger — likely needs a TUI dependency
  (e.g. `rich`/`textual`) the project doesn't currently carry, which per the
  project's own dependency-justification convention needs a real reason,
  not just symmetry with the web page.

Recommendation: ship Option A with the rest of this proposal; treat Option B
as a possible later upgrade only if the CLI surface turns out to see real
use — not something to build speculatively for a channel that may see the
least traffic of the three.

## Explicitly out of scope for v1

- Replacing or duplicating `_HELP_ENTRIES` / the command catalog.
- Backend/full-text search infrastructure (Elasticsearch, etc.) — not
  warranted at this content volume.
- Retrofitting every existing notification with a deep-link on day one —
  ship the platform + the motivating `book-vanished` entry, expand
  coverage opportunistically afterward (the same way ADRs get written
  after a decision, not spec'd out speculatively in advance).
- Populating the full glossary/FAQ content set — this doc proposes the
  *structure*; writing the actual entries (spacing, offside, cost basis,
  the MoE panel, the Gremlin, DMS, ADR-002's advisory-only firewall, etc.)
  is separate content work, sized once the structure is agreed.

## Open questions to resolve before implementation

1. `config/help/` vs. a dedicated top-level directory for content files.
2. Web layout: category-sidebar vs. single-page-with-TOC (worth a quick
   prototype of both).
3. CLI depth: Option A (one-shot, recommended) vs. Option B (interactive
   browser).
4. Does this become its own `roadmap.md` Stage now, or stay parked here
   until the operator decides to schedule it — per `future-ideas.md`'s own
   stated graduation process (idea → Stage when planned)? Recommendation:
   the latter; it's referenced from `docs/release/v1.1/operator-ux.md` in
   the meantime so it's discoverable through the existing register.
