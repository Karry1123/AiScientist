"""Stop interrupts planning and expert lookups without a live model or tools."""

import json
from dataclasses import replace
from threading import Event

import pytest

from aiscientist.agents import research_lab as rl
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness


EXPERTS = (rl.Specialist("Biologist", "genes"), rl.Specialist("Reviewer", "evidence"))


def _unexpected_chat(*_args):
    pytest.fail("unexpected scientist model call")


def _lab(tmp_path, complete, chat=None, tool=None):
    tools = [HarnessTool("literature_search", "Search papers",
                         {"type": "object", "properties": {}}, tool)] if tool else []
    return rl.ResearchLab(
        HarnessContext(decisions={}, workspace=tmp_path),
        rl.LabConfig(auto_select_skill=False, mode="team", max_meeting_concurrency=1),
        complete_fn=complete, scientist=ResearchHarness(catalog=tools, chat_fn=chat or _unexpected_chat))


def _calls():
    return {"tool_calls": [
        {"id": str(i), "type": "function",
         "function": {"name": "literature_search", "arguments": "{}"}}
        for i in range(2)]}


@pytest.mark.parametrize("stop_in", ["model", "tool", "retry"])
def test_stop_mid_meeting_prevents_next_tool_model_and_queued_expert(tmp_path, stop_in):
    stop = Event()
    calls = []
    events = []

    def chat(_messages, _tools):
        calls.append("model")
        if stop_in in ("model", "retry"):
            stop.set()
        if stop_in == "retry":
            raise RuntimeError("maximum context length exceeded")
        return _calls()

    def tool(_args, _ctx):
        calls.append("tool")
        stop.set()
        return {"status": "ok"}

    def complete(_messages):
        calls.append("fallback/critic/synthesis")
        return "opinion"

    lab = _lab(tmp_path, complete, chat, tool)
    lab._should_cancel = stop.is_set
    assert lab._team_meeting("q", "topic", EXPERTS, "design", events.append) == ""
    assert calls == (["model", "tool"] if stop_in == "tool" else ["model"])
    assert any(e["type"] == "team_meeting_cancelled" for e in events)
    assert not any(e["type"] == "expert_contribution" for e in events)


@pytest.mark.parametrize("stop_at", ["expert", "critic", "synthesis"])
def test_stop_in_plain_meeting_completion_ends_round(tmp_path, stop_at):
    stop = Event()
    calls = []

    def complete(messages):
        system = messages[0]["content"]
        stage = ("critic" if system == rl._MEETING_CRITIC_SYSTEM else
                 "synthesis" if system == rl._MEETING_SYNTH_SYSTEM else "expert")
        calls.append(stage)
        if stage == stop_at:
            stop.set()
        return '{"score": 0.1, "critique": "check evidence"}'

    lab = _lab(tmp_path, complete)
    lab._should_cancel = stop.is_set
    assert lab._team_meeting("q", "topic", EXPERTS, "design", lambda _e: None) == ""
    expected = ["expert"] if stop_at == "expert" else ["expert", "expert", "critic"]
    if stop_at == "synthesis":
        expected.append("synthesis")
    assert calls == expected


@pytest.mark.parametrize("stop_at", ["routing", "formation", "draft", "review"])
def test_stop_during_planning_returns_cancelled_without_another_completion(tmp_path, stop_at):
    stop = Event()
    calls = []
    events = []

    def complete(messages):
        system = messages[0]["content"]
        stage = ("routing" if system == rl._MODE_ROUTE_SYSTEM else
                 "formation" if system == rl._TEAM_FORM_SYSTEM else
                 "draft" if system.startswith(rl._PI_SYSTEM) else
                 "review" if system == rl._PLAN_REVIEW_CRITIC_SYSTEM else "scope")
        assert not stop.is_set(), "a model call started after Stop"
        calls.append(stage)
        if stage == stop_at:
            stop.set()
        if stage == "routing":
            return '{"mode": "team"}'
        if stage == "formation":
            return '[{"title": "Biologist", "expertise": "genes"}]'
        return json.dumps(["Search papers for haploinsufficient genes", "Validate gene evidence"])

    lab = _lab(tmp_path, complete)
    lab.config = replace(lab.config, mode="auto" if stop_at == "routing" else "team")
    # Exercise real formation/drafting; the meeting itself is covered above.
    lab._team_meeting = lambda *_a, **_k: "design"
    result = lab.run("What are haploinsufficient genes in RetiGene papers?",
                     on_event=events.append, should_cancel=stop.is_set)
    assert stop.is_set(), calls
    assert calls[-1] == stop_at
    assert result.rounds == [] and not result.converged
    assert "Stopped" in result.final_answer
    assert any(e["type"] == "run_cancelled" for e in events)


def test_stop_callback_is_reset_for_reused_lab(tmp_path):
    stop = Event()
    stop.set()
    lab = _lab(tmp_path, lambda _m: "unused")
    result = lab.run("q", should_cancel=stop.is_set)
    assert "Stopped" in result.final_answer
    lab._run_inner = lambda *_a, **_k: rl.LabResult("q", [], [], False, 0, "fresh run")
    assert lab.run("q").final_answer == "fresh run"
