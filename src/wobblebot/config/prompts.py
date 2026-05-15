"""Prompt-file loader for the Phase 3 strategy advisor.

Each MoE expert (and the optional arbitrator) points to a Markdown
file with YAML frontmatter via its config's ``prompt_file`` field
(see :class:`ExpertConfig.prompt_file` in ``advisor.py``). This
module loads those files and validates the metadata block.

File format::

    ---
    role: quant | risk | news | arbitrator | custom
    description: One-line summary shown in operator-facing output.
    response_schema: name-of-schema
    temperature_hint: 0.5
    ---

    System prompt body in Markdown. Operators edit this freely;
    the only mechanical contract is the frontmatter.

The loader returns a :class:`Prompt` model so callers can read
``prompt.body`` (the system prompt) and ``prompt.metadata.role``
(the role declared by the file) without re-parsing.

Per ADR-009, prompt files live under ``config/prompts/`` and are
gitignored if and only if they contain operator secrets — the
defaults checked into the repo are placeholder skeletons safe to
commit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import frontmatter
from pydantic import BaseModel, Field, field_validator

PromptRole = Literal["quant", "risk", "news", "arbitrator", "custom"]


class PromptMetadata(BaseModel):
    """Validated frontmatter block of a prompt file."""

    role: PromptRole
    description: str = Field(min_length=1)
    response_schema: str = Field(min_length=1)
    temperature_hint: float = Field(default=0.5, ge=0.0, le=2.0)

    class Config:
        frozen = True

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: str) -> str:
        return v.strip()


class Prompt(BaseModel):
    """A loaded prompt: validated metadata + raw body string."""

    metadata: PromptMetadata
    body: str = Field(min_length=1)
    source_path: Path

    class Config:
        frozen = True
        arbitrary_types_allowed = True


def load_prompt(path: Path) -> Prompt:
    """Read and validate a prompt file at ``path``.

    Args:
        path: Filesystem path to a Markdown file with YAML frontmatter.

    Returns:
        Validated :class:`Prompt`.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ValueError: Frontmatter is missing, the body is empty, or any
            required metadata field is absent / wrong type.
        pydantic.ValidationError: Frontmatter present but fails schema.
    """
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    parsed = frontmatter.load(str(path))
    if not parsed.metadata:
        raise ValueError(
            f"Prompt file {path} is missing the YAML frontmatter block "
            "(expected `---` delimited section at the top)"
        )

    body = parsed.content.strip()
    if not body:
        raise ValueError(f"Prompt file {path} has empty body — system prompt is required")

    metadata = PromptMetadata.model_validate(parsed.metadata)
    return Prompt(metadata=metadata, body=body, source_path=path)


__all__ = ["Prompt", "PromptMetadata", "PromptRole", "load_prompt"]
