"""Narration: the template is deterministic; the model only changes prose."""

from __future__ import annotations

import json

import pytest

from candlegraph.agents.base import Severity
from candlegraph.agents.narrator import AnthropicNarrator, TemplateNarrator
from tests.unit.agents.conftest import finding


def test_the_template_says_nothing_to_report_for_no_findings() -> None:
    assert TemplateNarrator().narrate("observer", []) == "observer: nothing to report."


def test_the_template_leads_with_status_and_counts_then_the_worst_findings() -> None:
    text = TemplateNarrator(highlights=1).narrate(
        "observer",
        [finding(Severity.OK, message="fine"), finding(Severity.WARNING, subject="WETH", message="7h gap")],
    )

    assert text.startswith("observer finished WARNING: 2 findings (1 warning, 1 ok).")
    assert "WETH: 7h gap" in text
    assert "fine" not in text, "OK findings are counted, not narrated"


def test_an_all_ok_report_is_one_sentence() -> None:
    text = TemplateNarrator().narrate("janitor", [finding(), finding()])

    assert text == "janitor finished OK: 2 findings (2 ok)."


def test_the_model_sees_only_the_findings_and_its_text_is_used() -> None:
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return "  All quiet apart from a gap on WETH.  "

    text = AnthropicNarrator("claude-x", complete=complete).narrate(
        "observer", [finding(Severity.WARNING, subject="WETH")]
    )

    assert text == "All quiet apart from a gap on WETH."
    assert "WETH" in prompts[0] and "Status: warning" in prompts[0]
    json.loads(prompts[0].split("Findings:\n", 1)[1])  # the findings travel as JSON


def test_a_failing_model_falls_back_to_the_template(caplog: pytest.LogCaptureFixture) -> None:
    def complete(_prompt: str) -> str:
        raise RuntimeError("no network")

    with caplog.at_level("WARNING", logger="candlegraph"):
        text = AnthropicNarrator("claude-x", complete=complete).narrate("observer", [finding(Severity.INFO)])

    assert text.startswith("observer finished INFO")
    assert "Narration via claude-x failed" in caplog.text


def test_an_empty_model_answer_falls_back_to_the_template() -> None:
    text = AnthropicNarrator("claude-x", complete=lambda _p: "   ").narrate("observer", [finding()])

    assert text == "observer finished OK: 1 findings (1 ok)."


def test_no_findings_never_calls_the_model() -> None:
    def complete(_prompt: str) -> str:
        raise AssertionError("should not be called")

    assert (
        AnthropicNarrator("claude-x", complete=complete).narrate("observer", [])
        == "observer: nothing to report."
    )


def test_agent_llm_model_actually_selects_the_model_narrator() -> None:
    """Regression, 2026-11-15: the setting was documented and ignored by both agents."""
    from candlegraph.agents.narrator import AnthropicNarrator, NarratorSelector, TemplateNarrator
    from tests.conftest import make_settings

    default = NarratorSelector(make_settings())
    assert default.model is None
    assert isinstance(default.build(), TemplateNarrator)

    configured = NarratorSelector(make_settings(agent_llm_model="claude-sonnet-5-5", anthropic_api_key="k"))
    assert configured.model == "claude-sonnet-5-5"
    chosen = configured.build()
    assert isinstance(chosen, AnthropicNarrator) and chosen.model == "claude-sonnet-5-5"


def test_the_observer_uses_the_configured_narrator(monkeypatch: pytest.MonkeyPatch) -> None:
    from candlegraph.agents.observer import run_observer
    from tests.conftest import FakeDatabase, make_settings

    built: list[str] = []

    class FakeSelector:
        def __init__(self, settings: object) -> None:
            built.append("called")

        def build(self) -> object:
            from candlegraph.agents.narrator import TemplateNarrator

            return TemplateNarrator()

    monkeypatch.setattr("candlegraph.agents.observer.graph.NarratorSelector", FakeSelector)
    run_observer(make_settings(), db=FakeDatabase(), sinks=[])

    assert built == ["called"]
