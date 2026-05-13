# Coding Guidelines & Conventions

This document defines how to write code in the WobbleBot project so that future you doesn’t hate past you.  Following a consistent style makes collaboration easier and reduces friction during code reviews.

## Language & Version

- Use **Python 3.13+** for all backend modules.
- Prefer the standard library unless there is a compelling reason to add a dependency.

## Project Structure

- `src/wobblebot/` – application code
  - `domain/` – core domain models and deterministic business logic
  - `ports/` – abstract interfaces (hexagonal ports)
  - `adapters/` – concrete implementations (e.g., Kraken API, SQLite DB, LLM adapter)
  - `services/` – orchestrators, schedulers, and higher‑level flows
  - `cli/` – command‑line entry points
  - `config/` – configuration schemas, loaders, and default values
- `tests/` – mirrors the `src/` structure with test files

Keep files focused.  If a module exceeds about 300–400 lines and feels like a junk drawer, consider splitting it into smaller modules.

## Style

- Use **black** for code formatting.  Consistent formatting reduces bike‑shedding.
- Use **isort** for import ordering (standard library, third‑party packages, local modules).
- Use **mypy** or **pyright** for type checking on the core modules.

General guidelines:

- Prefer **dataclasses** or **Pydantic models** for structured data.
- Avoid circular imports by respecting layer boundaries: domain should not import adapters.
- Keep functions short, composable, and side‑effect aware.  A function should do one thing well.

## Error Handling

- Raise domain‑specific exceptions in the domain layer (e.g., `ExposureLimitExceeded`).
- Wrap adapter failures in clear exception types and log the context (request parameters, endpoint called).
- Never swallow exceptions silently.  Log errors at the appropriate level (warning or error) with enough detail to aid debugging.

## Logging

- Use the project’s logging utilities; do not call `print()` directly.
- Log key decisions (e.g., “placing buy order”, “harvest proposal generated”).
- Log external API interactions at debug or info level.
- Log failures with stack traces at warning or error level.

## Testing

- Every non‑trivial module must have unit tests.
- Do not touch real external services in unit tests.  Use mocks or stubs.
- Integration tests may hit real services but must be clearly marked and skipped by default unless environment variables are set.

When writing new code, assume that “tests and documentation” are part of the work, not optional extras.

## Dependencies

- Keep the dependency list minimal.  Document new dependencies in pull requests and justify why they are necessary.
- Be skeptical of adding heavy frameworks.  Favour small, focused libraries that are easy to replace.

## Preferred Patterns

- Follow the hexagonal architecture: `Domain → Ports → Adapters`.
- Use dependency injection via constructors or simple factories to decouple modules.
- Implement core logic as pure functions where feasible to aid testability and determinism.

When you’re tempted to write clever code, consider writing clear code instead.  Clarity and maintainability trump cleverness.
