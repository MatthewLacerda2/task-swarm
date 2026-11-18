"""Sub-teams (a worker splitting its own task) and the planner (a stronger model drafting the plan)."""

from __future__ import annotations

import asyncio

import pytest

from conftest import COLLECTIONS, GOAL, FakeLLM, bash, call, lead_script, task, texts_of, tool_calls, worker
from taskswarm import config, orchestrator, plan
from taskswarm import run as runmod

OPUS = "claude-opus-5-5"


def splitter(pieces, summary="The pieces, put together.", early_report=False):
    """A worker that splits its task, waits for the pieces, then hands in the whole."""

    def step(messages):
        last = texts_of(messages[-1])
        said = tool_calls(messages)
        if not said:
            return [call("plan", tasks=pieces)]
        if early_report and len(said) == 1:
            return [call("report", summary="Too early.")]
        if "None of your sub-tasks" in last or "were dropped" in last:
            return [call("report", summary=summary)]
        return "Waiting for my sub-team."

    return step


# --- a worker splits its task -----------------------------------------------------------


async def test_worker_splits_its_task_and_hands_in_the_whole(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("big"), task("after", ["big"])]}),
        "big": splitter([task("x"), task("y", ["x"])], early_report=True),
        "big.x": worker(summary="x is done"),
        "big.y": worker(summary="y is done"),
        "after": worker(summary="after is done"),
    })
    assert await orchestrator.execute(run) == "done"
    assert orchestrator.summary(run)["tasks"] == {"big": "done", "after": "done", "big.x": "done", "big.y": "done"}
    assert run.db.task("big.y")["parent"] == "big" and run.db.task("big")["parent"] is None

    # each piece knows what it is a piece of, who gave it, and that it can't split again
    first = fake.seen["big.y"][0]
    assert "x is done" in first and "piece of: `big`" in first and "given to you by `big`" in first
    assert "can't be split" in first and "You may split it" in fake.seen["big"][0]
    # each piece is reviewed by itself, and the whole is reviewed knowing it was split
    assert {"big.x.review1", "big.y.review1", "big.review1"} <= set(fake.seen)
    assert "# Sub-tasks" in fake.seen["big.review1"][0] and "`big.x` (done)" in fake.seen["big.review1"][0]
    # the pieces report to the worker that split the task; the lead only ever sees that worker's report
    big, lead = "\n".join(fake.seen["big"]), "\n".join(fake.seen["lead"])
    assert "Task `big.x` (Task x) passed review" in big and "y is done" in big
    assert "The pieces, put together." in lead and "x is done" not in lead and "big.x" not in lead
    assert "The pieces, put together." in fake.seen["after"][0]
    # it could not hand in while its pieces were open
    assert "Your sub-tasks are still open: big.x, big.y" in big


def test_gates_on_splitting(make_run):
    run, _ = make_run({})
    assert plan.apply(run, GOAL | {"tasks": [task("a"), task("other")]})[0]
    run.db.update_task("a", status="running")

    def refused(inp, needle, owner="a"):
        ok, text = plan.apply(run, inp, owner=owner)
        assert not ok and needle in text, text
        assert run.db.children("a") == []

    refused({"goal": "mine", "tasks": [task("x"), task("y")]}, "only the lead sets the goal")
    refused({"collections": COLLECTIONS, "tasks": [task("x"), task("y")]}, "only the lead sets the goal")
    refused({"tasks": [task("x")]}, "can't be split into one piece")
    refused({"tasks": [task(f"p{i}") for i in range(7)]}, "the limit is 6")
    refused({"tasks": [task("x", ["other"]), task("y")]}, "not one of your tasks")
    refused({"tasks": [task("review1"), task("y")]}, "tasks[0]")

    ok, text = plan.apply(run, {"tasks": [task("x"), task("y", ["x"])]}, owner="a")
    assert ok and "`a.x` pending" in text and "blocked by a.x" in text
    assert plan.ready(run) == ["other", "a.x"]
    # a piece can't be split again, and its names work with or without the owner's prefix
    run.db.update_task("a.x", status="running")
    ok, text = plan.apply(run, {"tasks": [task("deep"), task("deeper")]}, owner="a.x")
    assert not ok and "a piece can't be split again" in text
    ok, text = plan.apply(run, {"cancel": ["a.y"], "tasks": [task("a.z", ["a.x"])]}, owner="a")
    assert ok and run.db.task("a.y")["status"] == "cancelled" and run.db.task("a.z")["parent"] == "a"

    # the lead doesn't manage a worker's pieces, but cancelling the task takes them with it
    ok, text = plan.apply(run, {"cancel": ["a.x"]})
    assert not ok and "you have no task `a.x`" in text
    assert plan.apply(run, {"cancel": ["a"]})[0]
    assert {t["id"]: t["status"] for t in run.db.descendants("a")} == {
        "a.x": "cancelled", "a.y": "cancelled", "a.z": "cancelled"}


def test_no_splitting_when_only_the_lead_may(make_run):
    run, _ = make_run({}, max_depth=1)
    assert plan.apply(run, GOAL | {"tasks": [task("a")]})[0]
    ok, text = plan.apply(run, {"tasks": [task("x"), task("y")]}, owner="a")
    assert not ok and "only the lead splits work" in text


def test_pieces_start_only_while_their_task_is_at_work(make_run):
    run, _ = make_run({})
    assert plan.apply(run, GOAL | {"tasks": [task("a")]})[0]
    run.db.update_task("a", status="running")
    assert plan.apply(run, {"tasks": [task("x"), task("y")]}, owner="a")[0]
    assert plan.ready(run) == ["a.x", "a.y"]
    run.db.update_task("a", status="reviewing")
    assert plan.ready(run) == []


async def test_a_failed_task_takes_its_pieces_with_it(make_run):
    no = [[call("verdict", passed=False, findings="The whole does not hold together at all.")]]
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("big")]}),
        # hands in before a slow piece is cancelled: cancel it, hand in, fail review
        "big": lambda messages: (
            [call("plan", tasks=[task("x"), task("slow", ["x"])])] if not tool_calls(messages)
            else [call("plan", cancel=["slow"], tasks=[task("z"), task("w", ["z"])]), ]
            if len(tool_calls(messages)) == 1 and "passed review" in texts_of(messages[-1])
            else [call("report", summary="Handed in with pieces missing.")]
            if "passed review" in texts_of(messages[-1]) or "did not pass" in texts_of(messages[-1])
            else "Waiting."),
        "big.x": worker(), "big.z": worker(), "big.w": worker(),
        "big.review1": no, "big.review2": no,
    }, max_attempts=1)
    assert await orchestrator.execute(run) == "done"
    tasks = orchestrator.summary(run)["tasks"]
    assert tasks["big"] == "failed" and tasks["big.slow"] == "cancelled"
    assert all(s in ("done", "cancelled") for t, s in tasks.items() if t != "big")
    assert "big.slow" not in fake.seen
    assert any("Task `big` (Task big) failed" in t for t in fake.seen["lead"])


async def test_a_sub_team_spends_its_task_budget(make_run):
    # every call costs about US$ 0.01; the task and everything under it may spend 0.035
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("big"), task("other")]}),
        "big": splitter([task("x"), task("y"), task("z")]),
        "big.x": worker(), "big.y": worker(), "big.z": worker(),
        "other": worker(),
    }, output_tokens=20_000, task_budget=0.035, agent_budget=1.0, lead_budget=1.0, run_budget=5.0)
    assert await orchestrator.execute(run) == "done"
    assert run.db.task("big")["status"] == "failed" and run.db.task("other")["status"] == "done"
    assert run.db.tree_spent("big") < 0.035 + 0.011           # the last call may land on the limit
    assert any("used up the task's budget" in (t["findings"] or "") for t in run.db.tasks())
    assert all(t["status"] in ("failed", "cancelled", "done") for t in run.db.descendants("big"))


async def test_hard_stop_with_a_waiting_worker_pauses_and_resumes(make_run):
    scripts = {
        "lead": lead_script(GOAL | {"tasks": [task("big")]}),
        "big": splitter([task("x"), task("y", ["x"])]),
        "big.x": worker(), "big.y": worker(),
    }
    run, _ = make_run(scripts, output_tokens=20_000, run_budget=0.05, hard_stop_ratio=1.2)
    assert await asyncio.wait_for(orchestrator.execute(run), 30) == "paused"
    assert run.db.task("big")["status"] == "running"
    run.db.conn.close()

    cfg = config.RunConfig(**(run.cfg.to_dict() | {"run_budget": 5.0}))
    probe = runmod.open_run(cfg, None)
    probe.db.set_meta("config", cfg.to_dict())
    probe.db.conn.close()
    run2 = runmod.open_run(cfg, FakeLLM(scripts))
    assert await asyncio.wait_for(orchestrator.execute(run2), 30) == "done"
    assert run2.db.task("big")["status"] == "done"
    run2.db.conn.close()


async def test_wind_down_drops_pieces_that_never_started(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("big")]}),
        "big": splitter([task("x"), task("y", ["x"]), task("z", ["y"])]),
        "big.x": worker(), "big.y": worker(), "big.z": worker(),
    }, output_tokens=20_000, run_budget=0.045, hard_stop_ratio=5.0, task_budget=5.0)
    assert await asyncio.wait_for(orchestrator.execute(run), 30) in ("done", "done_unverified")
    assert run.db.task("big.z")["status"] == "cancelled" and "big.z" not in fake.seen
    assert any("were dropped" in t for t in fake.seen["big"])
    ok, text = plan.apply(run, {"tasks": [task("late")]})
    assert not ok and "budget is used up" in text


# --- the planner -------------------------------------------------------------------------


async def test_a_stronger_model_drafts_the_plan(make_run):
    run, fake = make_run({
        "planner": [
            [call("report", summary="I will do it all myself.")],
            [call("plan", **(GOAL | {"collections": COLLECTIONS, "tasks": [task("a"), task("b", ["a"])]}))],
        ],
        "lead": lead_script(None),
        "a": worker(), "b": worker(),
    }, plan_model=OPUS)
    assert await orchestrator.execute(run) == "done"
    assert fake.models["planner"] == OPUS and fake.models["lead"] == fake.models["a"] == "claude-haiku-5-5"
    assert run.db.agent("planner")["role"] == "planner"
    # the planner only plans, and its calls are billed at its own prices
    assert "`report` is not for your role (planner)" in "\n".join(fake.seen["planner"])
    assert fake.calls.count("planner") == 2
    per_call = (500 * 4.00 + 2500 * 0.20 + 200 * 20.00) / 1e6
    assert run.db.agent("planner")["spent"] == pytest.approx(2 * per_call)
    # the lead starts with the plan in hand and never has to write one
    first = fake.seen["lead"][0]
    assert "The planner drafted this plan" in first and "## `b`: Task b (after a)" in first
    assert "Open three stored records" in first and "`companies`" in first
    assert orchestrator.summary(run)["tasks"] == {"a": "done", "b": "done"}


async def test_lead_plans_when_the_planner_gives_nothing(make_run):
    run, fake = make_run({
        "planner": ["Let me think.", "Still thinking.", "Hm."],
        "lead": lead_script(GOAL | {"tasks": [task("a")]}),
        "a": worker(),
    }, plan_model=OPUS)
    assert await orchestrator.execute(run) == "done"
    assert "planner drafted" not in fake.seen["lead"][0]
    assert sum("ended your turn without handing in" in t and "`plan`" in t for t in fake.seen["planner"]) == 2


async def test_no_planner_when_the_budget_is_too_small(make_run):
    run, fake = make_run({"lead": lead_script(GOAL | {"tasks": [task("a")]}), "a": worker()},
                         plan_model=OPUS, run_budget=0.50)
    assert await orchestrator.execute(run) == "done"
    assert run.db.agent("planner") is None and "planner" not in fake.seen
    assert run.db.one("SELECT 1 FROM events WHERE kind = 'planner_skipped'")


def test_every_priced_model_has_sane_prices():
    for name, p in config.MODELS.items():
        assert p.cache_write_5m == pytest.approx(1.25 * p.input) and p.cache_write_1h == pytest.approx(2 * p.input), name
        assert p.cache_read < p.input < p.output, name


# --- waiting -----------------------------------------------------------------------------


async def test_an_owner_cannot_wait_inside_a_command(make_run):
    def react(last):
        if "Plan saved" in last:
            return [bash("sleep 120; cat /task/store/schema.json"), bash("python3 -c 'import time; time.sleep(60)'"),
                    bash("sleep 2; echo a short pause is fine")]

    def splitting(messages):
        if "Plan saved" in texts_of(messages[-1]):
            return [bash("sleep 90")]
        return splitter([task("x"), task("y")])(messages)

    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("big"), task("plain")]}, react=react),
        "big": splitting, "big.x": worker(), "big.y": worker(),
        "plain": [[bash("sleep 16; echo a worker with nothing under it may sleep")], [call("report", summary="ok")]],
    })
    assert await asyncio.wait_for(orchestrator.execute(run), 60) == "done"
    lead, big = "\n".join(fake.seen["lead"]), "\n".join(fake.seen["big"])
    assert "this command sleeps 120 seconds while you have tasks open (big, plain)" in lead
    assert "this command sleeps 60 seconds" in lead and "a short pause is fine" in lead
    assert "this command sleeps 90 seconds while you have tasks open (big.x, big.y)" in big
    assert "may sleep" in "\n".join(fake.seen["plain"])


# --- a plan the user gives with the task ----------------------------------------------------


async def test_a_given_plan_skips_the_planner_and_goes_through_the_same_gates(make_run):
    from taskswarm import status

    first, fake1 = make_run({
        "planner": [[call("plan", **GOAL, collections=COLLECTIONS, tasks=[task("find"), task("check", ["find"])])]],
        "lead": lead_script(None), "find": worker(summary="found"), "check": worker(summary="checked"),
    }, run_id="first", plan_model=OPUS, run_budget=5.0)
    assert await orchestrator.execute(first) == "done"
    exported = status.first_plan("first")
    assert [t["id"] for t in exported["tasks"]] == ["find", "check"] and exported["goal"] == GOAL["goal"]

    # the same plan, handed to a new run: no planner call, although a planner is set and would fit
    run, fake = make_run({"lead": lead_script(None), "find": worker(summary="found"),
                          "check": worker(summary="checked")}, run_id="second", plan_model=OPUS, run_budget=5.0)
    ok, text = plan.give(run, exported)
    assert ok, text
    assert await orchestrator.execute(run) == "done"
    assert "planner" not in fake.calls and "planner" in fake1.calls
    lead = fake.seen["lead"][0]
    assert "The user gave this plan with the task" in lead and "## `check`: Task check (after find)" in lead
    assert orchestrator.summary(run)["tasks"] == {"find": "done", "check": "done"}

    # a plan that breaks a gate is refused whole, with every problem named
    bad, _ = make_run({}, run_id="third")
    ok, text = plan.give(bad, GOAL | {"tasks": [task("a", ["missing"]), task("b", brief="too short")]})
    assert not ok and "missing" in text and "brief" in text
    assert bad.db.tasks() == [] and bad.db.get_meta("plan_from") is None
    assert plan.give(bad, ["not", "a", "plan"])[0] is False
