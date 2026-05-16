"""Tests for the prompt-file loader (``wobblebot.config.prompts``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wobblebot.config.prompts import Prompt, PromptMetadata, load_prompt

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_prompt(
    path: Path,
    *,
    role: str = "quant",
    description: str = "Test prompt",
    response_schema: str = "advisor_recommendation_v1",
    temperature_hint: float | None = 0.5,
    body: str = "You are a test expert.\nDo test things.",
) -> Path:
    parts = [
        "---",
        f"role: {role}",
        f"description: {description}",
        f"response_schema: {response_schema}",
    ]
    if temperature_hint is not None:
        parts.append(f"temperature_hint: {temperature_hint}")
    parts.append("---")
    parts.append("")
    parts.append(body)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLoadPromptHappyPath:
    def test_loads_valid_quant_prompt(self, tmp_path: Path) -> None:
        path = _write_prompt(tmp_path / "q.md")
        prompt = load_prompt(path)
        assert isinstance(prompt, Prompt)
        assert prompt.metadata.role == "quant"
        assert prompt.metadata.description == "Test prompt"
        assert prompt.metadata.response_schema == "advisor_recommendation_v1"
        assert prompt.metadata.temperature_hint == 0.5
        assert "You are a test expert." in prompt.body
        assert prompt.source_path == path

    def test_temperature_hint_defaults_to_05_when_omitted(self, tmp_path: Path) -> None:
        path = _write_prompt(tmp_path / "no_temp.md", temperature_hint=None)
        prompt = load_prompt(path)
        assert prompt.metadata.temperature_hint == 0.5

    def test_body_is_stripped(self, tmp_path: Path) -> None:
        path = _write_prompt(tmp_path / "padded.md", body="\n\nbody text\n\n")
        prompt = load_prompt(path)
        assert prompt.body == "body text"

    @pytest.mark.parametrize("role", ["quant", "risk", "news", "arbitrator", "operator", "custom"])
    def test_accepts_every_documented_role(self, tmp_path: Path, role: str) -> None:
        path = _write_prompt(tmp_path / f"{role}.md", role=role)
        prompt = load_prompt(path)
        assert prompt.metadata.role == role


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestLoadPromptErrors:
    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.md"
        with pytest.raises(FileNotFoundError, match="Prompt file not found"):
            load_prompt(missing)

    def test_missing_frontmatter_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "no_fm.md"
        path.write_text("Just a body, no frontmatter.\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing the YAML frontmatter"):
            load_prompt(path)

    def test_empty_body_rejected(self, tmp_path: Path) -> None:
        path = _write_prompt(tmp_path / "empty.md", body="")
        with pytest.raises(ValueError, match="empty body"):
            load_prompt(path)

    def test_invalid_role_rejected(self, tmp_path: Path) -> None:
        path = _write_prompt(tmp_path / "bad_role.md", role="not-a-real-role")
        with pytest.raises(Exception):  # pydantic ValidationError
            load_prompt(path)

    def test_temperature_hint_out_of_range_rejected(self, tmp_path: Path) -> None:
        path = _write_prompt(tmp_path / "hot.md", temperature_hint=5.0)
        with pytest.raises(Exception):  # pydantic ValidationError
            load_prompt(path)

    def test_missing_required_metadata_rejected(self, tmp_path: Path) -> None:
        # Frontmatter present but missing description + response_schema
        path = tmp_path / "incomplete.md"
        path.write_text(
            "---\nrole: quant\n---\n\nbody\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception):  # pydantic ValidationError
            load_prompt(path)


# ---------------------------------------------------------------------------
# Smoke-test the four shipped prompts
# ---------------------------------------------------------------------------


class TestShippedPrompts:
    """The four prompts in ``config/prompts/`` must always load. Anyone
    editing them must keep the frontmatter contract intact."""

    @pytest.fixture
    def prompts_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "config" / "prompts"

    @pytest.mark.parametrize(
        ("filename", "expected_role", "expected_schema"),
        [
            ("quant.md", "quant", "advisor_recommendation_v1"),
            ("risk.md", "risk", "advisor_recommendation_v1"),
            ("news.md", "news", "advisor_recommendation_v1"),
            ("arbitrator.md", "arbitrator", "advisor_recommendation_v1"),
            ("operator.md", "operator", "operator_intent_v1"),
        ],
    )
    def test_shipped_prompt_loads(
        self,
        prompts_dir: Path,
        filename: str,
        expected_role: str,
        expected_schema: str,
    ) -> None:
        prompt = load_prompt(prompts_dir / filename)
        assert prompt.metadata.role == expected_role
        assert prompt.metadata.description
        assert prompt.metadata.response_schema == expected_schema
        assert prompt.body  # non-empty


class TestPromptMetadataModel:
    def test_metadata_is_frozen(self) -> None:
        meta = PromptMetadata(
            role="quant",
            description="x",
            response_schema="advisor_recommendation_v1",
        )
        with pytest.raises(Exception):  # pydantic ValidationError on assignment
            meta.role = "risk"  # type: ignore[misc]
