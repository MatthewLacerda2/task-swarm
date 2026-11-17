"""The plan's gates: nothing changes unless the whole change is sound."""

from __future__ import annotations

from conftest import COLLECTIONS, task
from taskswarm import plan

GOAL = {"goal": "A list of the companies that make the thing.", "done_when": "Every company has a source that names it."}


def test_first_plan(make_run):
    run, _ = make_run({})
    ok, text = plan.apply(run, GOAL | {"collections": COLLECTIONS, "tasks": [task("a"), task("b", ["a"])]})
    assert ok, text
    assert plan.ready(run) == ["a"]
    assert "companies" in run.store.schema and "blocked by a" in text
    run.db.update_task("a", status="done")
    assert plan.ready(run) == ["b"]


def test_problems_are_all_reported_and_nothing_changes(make_run):
    run, _ = make_run({})
    ok, text = plan.apply(run, {"tasks": [
        task("a", ["ghost"]),
        task("Bad Id"),
        task("lead"),
        task("c", brief="do it"),
        task("d", verify=""),
        task("e", ["e"]),
    ], "collections": {"companies": {"fields": {}}}})
    assert not ok
    for needle in ("`goal` is missing", "`done_when` is missing", "`ghost`, which doesn't exist", "tasks[1]",
                   "tasks[2]", "too short to stand alone", "`verify` is missing", "blocked by itself",
                   "at least one field"):
        assert needle in text, needle
    assert run.db.tasks() == [] and run.db.get_meta("goal") is None and run.store.schema == {}


def test_cycle(make_run):
    run, _ = make_run({})
    ok, text = plan.apply(run, GOAL | {"tasks": [task("a", ["c"]), task("b", ["a"]), task("c", ["b"])]})
    assert not ok and "in a circle" in text


def test_started_tasks_cannot_be_redefined_but_pending_can(make_run):
    run, _ = make_run({})
    assert plan.apply(run, GOAL | {"tasks": [task("a"), task("b", ["a"])]})[0]
    run.db.update_task("a", status="running")
    ok, text = plan.apply(run, {"tasks": [task("a", title="again")]})
    assert not ok and "can't be redefined" in text
    ok, _ = plan.apply(run, {"tasks": [task("b", title="changed")]})
    assert ok and run.db.task("b")["title"] == "changed" and run.db.task("b")["seq"] == 2


def test_cancel_and_failed_blockers(make_run):
    run, _ = make_run({})
    assert plan.apply(run, GOAL | {"tasks": [task("a"), task("b", ["a"]), task("c")]})[0]
    ok, text = plan.apply(run, {"cancel": ["a"]})
    assert not ok and "blocked by `a`, which is cancelled" in text
    run.db.update_task("a", status="failed")
    assert plan.stuck(run) == ["b"]
    ok, text = plan.apply(run, {"tasks": [task("b", ["a"])]})
    assert not ok and "will never unblock" in text
    ok, text = plan.apply(run, {"tasks": [task("a2"), task("b", ["a2"])], "cancel": ["c"]})
    assert ok, text
    assert plan.stuck(run) == [] and run.db.task("c")["status"] == "cancelled"


def test_task_limit(make_run):
    run, _ = make_run({}, max_tasks=2)
    ok, text = plan.apply(run, GOAL | {"tasks": [task("a"), task("b"), task("c")]})
    assert not ok and "the limit is 2" in text
