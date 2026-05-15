# Contributing to WobbleBot

Thanks for the interest. WobbleBot is a hobby project run by a single
maintainer; contributions are welcome but please read this whole doc
before opening a PR — there are non-obvious constraints that come from
the safety design.

## Before you write code

1. **Check the current phase and stage** in
   [`docs/planning/roadmap.md`](docs/planning/roadmap.md). Phases are
   strictly ordered; don't propose Phase N+1 features until Phase N
   is closed.
2. **Skim the ADRs** in
   [`docs/architecture/decisions.md`](docs/architecture/decisions.md).
   They're short. Most "should we add an abstraction for X?" questions
   have already been answered (often "no, see ADR-N").
3. **Read the safety constraints** in
   [`docs/architecture/constraints.md`](docs/architecture/constraints.md).
   Financial-power fragmentation is the load-bearing invariant: no
   single module controls both trading and money movement. PRs that
   blur that line will be declined regardless of how clean the code
   is.

## Discuss first

For anything beyond a typo fix, **open an issue first** and we'll
agree on the approach before you spend time. The five minutes spent
aligning saves the hour spent reworking. Especially relevant for:

- New ports, adapters, or services.
- Changes to the engine's safety cap arithmetic.
- Anything in the LLM advisor surface.
- New runtime dependencies.

## Coding standards

- **Python 3.13+.** Use `str | None`, `list[X]`, `match` statements —
  no `Optional` / `List` imports needed.
- **Hexagonal layers are load-bearing.** `domain/` must never import
  from `adapters/`, `services/`, or `cli/`. Dependencies flow inward
  only. See `CLAUDE.md` for the full rules.
- **No `print()`.** Use the project logger
  (`wobblebot.config.logging.configure_logging`).
- **Pydantic v2 models** for structured data. Frozen where
  appropriate; field validators where input needs normalization.
- **Async ports** (`ExchangePort`, etc.) — use `pytest-asyncio` for
  tests.
- **Default to writing no comments.** Only add a comment when the
  WHY is non-obvious. Don't explain what the code does — well-named
  identifiers do that.
- **Line length 100** (black + isort + pylint configured to this).
- **Keep files under ~300-400 lines.** Split when a module turns
  into a junk drawer.

Full guide: [`docs/implementation/coding-guidelines.md`](docs/implementation/coding-guidelines.md).

## Testing standards

- **No real network calls in unit tests.** Use mocks/stubs;
  `httpx.MockTransport` is the canonical seam for `KrakenAdapter`.
- **Integration tests carry the `integration` marker** and are
  excluded from the default `pytest` run via `addopts`. Run them
  explicitly with `pytest -m integration`.
- **Don't lower the test bar to make code pass.** If a test fails,
  fix the code. If a test is wrong, fix the test — but justify the
  change with concrete reasoning, not convenience.
- **Test real behavior, not the impossible.** Don't write
  hypothetical edge cases that can't happen given the system's
  constraints.

## Pre-commit hook

Install the repo's pre-commit hook on a fresh clone:

```bash
./scripts/install-hooks.sh        # macOS/Linux
scripts\install-hooks.ps1         # Windows PowerShell
```

This points `core.hooksPath` at `.githooks/`, enabling gitleaks +
PII pattern check + author-identity guard. Without it you only get
the global pre-commit hook, which lacks the PII / identity checks
this repo requires.

## Submitting a change

1. **One concept per PR.** A bug fix and a refactor and a new
   feature are three PRs, not one.
2. **Conventional commit prefix.** Look at recent `git log` to match
   the style — typically `deps(...)`, `fix(...)`, `Audit slice N: ...`,
   `Stage X.Y: ...`, etc.
3. **Update CHANGELOG.md** — append your entry under the
   `[Unreleased]` heading in the relevant phase section.
4. **Run the full check suite locally:**
   ```bash
   make check    # black + isort + mypy + pylint + pytest
   ```
   pylint must stay at 10.00/10. mypy must stay clean (strict mode).
5. **Don't bypass commit hooks** (`--no-verify`, `--no-gpg-sign`,
   etc.) without an explicit reason in the PR description. If a hook
   fails, investigate and fix the underlying issue.

## Reporting bugs

Open an issue with:

- What you expected to happen.
- What actually happened.
- The exact command / scenario that reproduces it.
- Affected commit SHA or branch.

For security vulnerabilities, **do not open a public issue** —
follow [`SECURITY.md`](SECURITY.md) instead.

## License

By contributing, you agree that your contributions will be licensed
under the same MIT License that covers the project (see
[`LICENSE`](LICENSE)).
