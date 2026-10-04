"""Orchestrator tests: the human-in-the-loop path and the HTTP surface.

The interesting property is not "does the endpoint return 200" but "does the run
actually hold its place while a person thinks". These tests park a real run,
check that it is genuinely suspended, answer it, and check that it carried on.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent.runtime import ApprovalAnswer, LiveInteraction
from orchestrator.app import create_app
from orchestrator.events import EventBus
from orchestrator.runs import RunManager, RunSession, _claimed_bill


# --------------------------------------------------------------------------
# LiveInteraction
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ask_parks_until_a_human_answers():
    live = LiveInteraction(timeout_seconds=5)
    task = asyncio.create_task(live.ask(None, "Which amount?", ["7275", "7995"]))
    await asyncio.sleep(0)          # let it reach the await

    assert task.done() is False, "the run must actually block, not guess"
    assert live.pending()[0]["question"] == "Which amount?"
    question_id = live.pending()[0]["id"]

    assert live.respond(question_id, "7995") is True
    assert await asyncio.wait_for(task, timeout=2) == "7995"
    assert live.pending() == [], "an answered question must stop blocking"
    assert live.questions[0].answered == "7995"


@pytest.mark.asyncio
async def test_approval_parks_and_parks_only_once():
    live = LiveInteraction(timeout_seconds=5)
    task = asyncio.create_task(live.ask_approval(
        None, action="submit bill", fingerprint="fp-1", justification="over threshold"))
    await asyncio.sleep(0)

    pending = live.pending()
    assert pending[0]["kind"] == "approval"
    assert pending[0]["fingerprint"] == "fp-1"

    live.respond(pending[0]["id"], "approve")
    answer = await asyncio.wait_for(task, timeout=2)
    assert isinstance(answer, ApprovalAnswer) and answer.granted is True

    # A late double-click must not raise or invent a second decision.
    assert live.respond(pending[0]["id"], "approve") is False


@pytest.mark.asyncio
async def test_approval_denial_is_honoured():
    live = LiveInteraction(timeout_seconds=5)
    task = asyncio.create_task(live.ask_approval(
        None, action="submit", fingerprint="fp", justification="risky"))
    await asyncio.sleep(0)
    live.respond(live.pending()[0]["id"], "deny")
    answer = await asyncio.wait_for(task, timeout=2)
    assert answer.granted is False


@pytest.mark.asyncio
async def test_unanswered_approval_times_out_as_denied():
    """The safety-critical default: nobody answered => no approval."""
    live = LiveInteraction(timeout_seconds=0.05)
    answer = await live.ask_approval(
        None, action="submit", fingerprint="fp", justification="risky")
    assert answer.granted is False
    assert "timed out" in answer.reason


@pytest.mark.asyncio
async def test_unanswered_question_abstains_rather_than_invents():
    live = LiveInteraction(timeout_seconds=0.05)
    assert await live.ask(None, "Which amount?", ["7275", "7995"]) == ""


@pytest.mark.asyncio
async def test_paused_time_is_recorded():
    """The loop credits parked time back against its deadline."""
    live = LiveInteraction(timeout_seconds=5)
    task = asyncio.create_task(live.ask(None, "q?", []))
    await asyncio.sleep(0.12)
    live.respond(live.pending()[0]["id"], "yes")
    await asyncio.wait_for(task, timeout=2)
    assert live.paused_seconds >= 0.1


@pytest.mark.asyncio
async def test_release_all_unblocks_a_stuck_run():
    live = LiveInteraction(timeout_seconds=30)
    task = asyncio.create_task(live.ask(None, "q?", []))
    await asyncio.sleep(0)
    live.release_all("")
    assert await asyncio.wait_for(task, timeout=2) == ""


# --------------------------------------------------------------------------
# EventBus
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bus_replays_history_then_streams_live():
    bus = EventBus()
    bus.publish("step", {"index": 1})
    bus.publish("step", {"index": 2})
    queue = bus.subscribe()
    bus.publish("step", {"index": 3})

    seen = [bus.history()[i]["data"]["index"] for i in range(2)]
    live = [(await asyncio.wait_for(queue.get(), timeout=1))["data"]["index"]]
    assert seen == [1, 2]
    assert live == [3], "a late subscriber must not miss anything"


@pytest.mark.asyncio
async def test_bus_sheds_events_for_a_stalled_reader():
    """A frozen tab must not apply backpressure to the agent."""
    bus = EventBus(queue_size=3)
    queue = bus.subscribe()
    for i in range(10):
        bus.publish("step", {"index": i})
    assert queue.qsize() == 3


def test_bus_replay_from_seq_skips_seen_events():
    bus = EventBus()
    for i in range(5):
        bus.publish("step", {"index": i})
    # seq is 1-based, so "after seq 3" means the 4th and 5th events.
    assert [e["data"]["index"] for e in bus.replay_from(3)] == [3, 4]


@pytest.mark.parametrize(
    "claim, expected",
    [
        ({"values": {"bills": [{"invoice_number": "HL-2291",
                                 "vendor": "Hooli Cloud Services"}]}},
         ("HL-2291", "Hooli Cloud Services")),
        ({"invoice": "HL-2260", "vendor": "Acme Corp"},
         ("HL-2260", "Acme Corp")),
        ({"values": {"bills": []}}, ("", "")),
        ({"values": {"bills": [{"number": "IN-5570"}]}}, ("IN-5570", "")),
        ({}, ("", "")),
    ],
)
def test_claimed_bill_survives_claim_shape_changes(claim, expected):
    """The claim schema has already moved once; a quiet skip here would mean the
    second-look check silently stops happening."""
    assert _claimed_bill(claim) == expected


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    import dataclasses

    from common.config import get_settings
    from orchestrator.runs import RunManager

    # Settings is frozen, so swap in a copy that writes into tmp_path rather than
    # letting a test scribble on the real runs/ directory.
    settings = dataclasses.replace(get_settings(), runs_dir=tmp_path)
    app = create_app(settings=settings, manager=RunManager(settings))
    with TestClient(app) as test_client:
        test_client.settings = settings
        yield test_client


def test_config_and_examples_are_served(client):
    body = client.get("/api/config").json()
    assert body["examples"], "the UI needs starting points"
    assert body["world_today"] == "2026-04-01"


def test_unknown_run_is_404_not_500(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404


def test_answering_a_question_that_is_not_open_is_409(client, tmp_path):
    """The common UI bug is a stale modal posting into a finished run."""
    settings = client.settings
    session = RunSession("run-test-404", "goal", settings)
    client.app.state.runs._sessions[session.run_id] = session
    response = client.post(f"/api/runs/{session.run_id}/answer",
                           json={"question_id": "q-nonexistent", "answer": "yes"})
    assert response.status_code == 409


def test_artifact_path_traversal_is_refused(client, tmp_path):
    settings = client.settings
    session = RunSession("run-test-405", "goal", settings)
    (session.run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    client.app.state.runs._sessions[session.run_id] = session
    for bad in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd"):
        response = client.get(f"/api/runs/{session.run_id}/artifact/{bad}")
        assert response.status_code in (400, 404), f"{bad} was not refused"


def test_session_summary_shape(client):
    settings = client.settings
    session = RunSession("run-test-406", "enter a bill", settings)
    client.app.state.runs._sessions[session.run_id] = session
    body = client.get(f"/api/runs/{session.run_id}").json()
    for key in ("run_id", "goal", "status", "running", "pending", "questions"):
        assert key in body, f"UI depends on {key!r}"