"""Whole runs with a scripted model: the plan, hand-offs, reviews, rework, the store,
messages, the budget and resuming. Real sandboxes, no API calls."""

from __future__ import annotations

import json
import os

from conftest import (COLLECTIONS, DELIVER, GOAL, FakeLLM, bash, call, lead_script, record, stored, task,
                      texts_of, tool_calls, worker)
from taskswarm import config, orchestrator
from taskswarm import run as runmod

async def test_plan_to_deliverable(make_run):
    plan_input = GOAL | {"collections": COLLECTIONS, "tasks": [task("find"), task("check", ["find"])]}
    run, fake = make_run({
        "lead": lead_script(plan_input),
        "find": worker(record("Acme", employees=12), {"name": "NoSource Ltd"}, summary="Found Acme; one had no source."),
        "check": worker(summary="Acme checks out."),
    })
    assert await orchestrator.execute(run) == "done"

    out = orchestrator.summary(run)
    assert out["tasks"] == {"find": "done", "check": "done"} and out["store"] == {"companies": 1}
    assert open(out["deliverable"]).read() == "Acme makes it.\n"
    [rec] = stored(run)
    assert rec["name"] == "Acme" and rec["_task"] == "find"

    # the rejected record came back to the worker with the reason
    assert any("`source` is required" in t for t in fake.seen["find"])
    # the second task started after the first passed, and got its report, not its conversation
    first = fake.seen["check"][0]
    assert "Found Acme; one had no source." in first and "/task/work/find/" in first and "thinks" not in first
    # the reviewer saw the brief, what to verify and the report
    review = fake.seen["find.review1"][0]
    assert "Open three stored records" in review and "Found Acme" in review
    # the lead was told each result, and the final reviewer saw the deliverable's path
    assert any("`find`" in t and "passed review" in t for t in fake.seen["lead"])
    assert "/task/deliverable/answer.md" in fake.seen["final.review1"][0]
    # everyone is one of the three roles and every call is accounted for
    assert {a["id"]: a["role"] for a in run.db.agents()} == {
        "lead": "lead", "find": "worker", "find.review1": "reviewer", "check": "worker",
        "check.review1": "reviewer", "final.review1": "reviewer"}
    assert abs(run.db.total_spent() - sum(a["spent"] for a in run.db.agents())) < 1e-9


async def test_failed_review_goes_back_to_the_worker(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find")]}),
        "find": [
            [call("report", summary="Done, I think.")],
            [call("store_write", collection="companies", records=[record("Acme")])],
            [call("report", summary="Now stored Acme.")],
        ],
        "find.review1": [[call("verdict", passed=False, findings="The store is empty: nothing was written.")]],
    })
    assert await orchestrator.execute(run) == "done"
    t = run.db.task("find")
    assert t["status"] == "done" and t["attempts"] == 2 and t["summary"] == "Now stored Acme."
    assert any("The store is empty" in x and "report 1 of at most 3" in x for x in fake.seen["find"])
    assert "find.review2" in fake.seen and "review 2 of at most 3" in fake.seen["find.review2"][0]


async def test_task_that_never_passes_fails_and_the_lead_replans(make_run):
    plan_input = GOAL | {"collections": COLLECTIONS, "tasks": [task("find"), task("check", ["find"])]}

    def react(last):
        if "Task `find` (Task find) failed" in last:
            assert "Can never start because of a failed task: check" in last
            return [call("plan", cancel=["check"], tasks=[task("find2")])]

    no = [[call("verdict", passed=False, findings="Still nothing stored, as before.")]]
    run, fake = make_run({
        "lead": lead_script(plan_input, react=react),
        "find": [[call("report", summary=f"Try {n}.")] for n in range(1, 5)],
        "find.review1": no, "find.review2": no,
        "find2": worker(record("Acme")),
    }, max_attempts=2)
    assert await orchestrator.execute(run) == "done"
    assert orchestrator.summary(run)["tasks"] == {"find": "failed", "check": "cancelled", "find2": "done"}
    assert "check" not in fake.seen


async def test_roles_are_enforced(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("find")]}),
        "find": [
            [call("plan", goal="mine now", done_when="never", tasks=[])],
            [call("verdict", passed=True, findings="I pass myself, it is all fine.")],
            [call("finish", path="x", summary="done")],
            [call("report", summary="Did my own task.")],
        ],
        "find.review1": [
            [call("report", summary="I am the worker now.")],
            [call("store_write", collection="companies", records=[])],
            [call("verdict", passed=True, findings="Checked the report against the brief.")],
        ],
    })
    assert await orchestrator.execute(run) == "done"
    seen = "\n".join(fake.seen["find"])
    assert "only the lead sets the goal" in seen
    assert "`verdict` is not for your role" in seen and "`finish` is not for your role" in seen
    assert "`report` is not for your role (reviewer)" in "\n".join(fake.seen["find.review1"])
    assert run.db.get_meta("goal") == GOAL["goal"]


async def test_finish_is_refused_while_tasks_are_open(make_run):
    def lead(messages):
        said = [b for m in messages if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
        if not said:
            return [call("finish", path="answer.md", summary="early")]
        if len(said) == 1:
            return [call("plan", **(GOAL | {"tasks": [task("a"), task("b", ["a"])]})),
                    call("finish", path="answer.md", summary="early")]
        if "No task is running" in texts_of(messages[-1]):
            return [bash("echo done > answer.md"), call("finish", path="/task/work/lead/answer.md", summary="now")]
        return "Waiting."

    run, fake = make_run({"lead": lead, "a": worker(), "b": worker()})
    assert await orchestrator.execute(run) == "done"
    seen = "\n".join(fake.seen["lead"])
    assert "Set the plan first" in seen and "Tasks have not started: a, b" in seen


async def test_deliverable_and_imports_only_from_own_folder(make_run):
    secret = config.RUNS_ROOT.parent / "secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"name": "leaked"}\n')
    lines = "\n".join(json.dumps(r) for r in [record("Acme"), record("Bolt"), {"name": "x"}]) + "\nnot json\n"
    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find")]},
                            deliver=f"ln -s {secret} answer.md; echo linked"),
        "find": [
            [bash(f"cat > data.jsonl <<'EOF'\n{lines}EOF\nln -s {secret} link.jsonl; ln -s / root")],
            [call("store_write", collection="companies", path="link.jsonl")],
            [call("store_write", collection="companies", path="root/etc/hostname")],
            [call("store_write", collection="companies", path="../lead/x.jsonl")],
            [call("store_write", collection="companies", path="/etc/hostname")],
            [call("store_write", collection="companies", path="data.jsonl")],
            [call("report", summary="Imported two.")],
        ],
    }, max_final_reviews=1)
    # the lead's deliverable is a symlink out of its folder: finish refuses, the run can't end well
    assert await orchestrator.execute(run) == "stalled"
    seen = fake.seen["find"]
    assert sum("Can't read that file" in t for t in seen) == 4
    assert any("2 accepted, 2 rejected" in t and "not valid JSON" in t for t in seen)
    assert [r["name"] for r in stored(run)] == ["Acme", "Bolt"]
    assert any("Can't read the deliverable" in t for t in fake.seen["lead"])
    assert list((run.cfg.run_dir / "deliverable").iterdir()) == []


async def test_messages(make_run):
    def react(last):
        if "[Message from `find`]" in last and "Which year?" in last:
            return [call("send_message", to="find", message="2025 only.")]
        if "No task is running" in last:      # the task is closed: its worker can still be asked
            return [call("send_message", to="find", message="One question after your report: which site was best?"),
                    call("send_message", to="nobody", message="hello?")]
        if "example.com was best." in last:
            return [bash(DELIVER)]

    def find(messages):
        last = texts_of(messages[-1])
        if not tool_calls(messages):
            return [call("send_message", to="lead", message="Which year?")]
        if "2025 only." in last:
            return [call("report", summary="Did 2025.")]
        if "which site was best" in last:
            return [call("report", summary="again"), call("send_message", to="lead", message="example.com was best.")]
        if any(b["name"] == "report" for b in tool_calls(messages)):
            return "Nothing more."
        return [bash("sleep 0.2; echo waiting for the answer")]

    run, fake = make_run({"lead": lead_script(GOAL | {"tasks": [task("find")]}, react=react), "find": find})
    assert await orchestrator.execute(run) == "done"
    assert run.db.task("find")["summary"] == "Did 2025."          # a closed task can't be handed in again
    assert any("already handed in" in t for t in fake.seen["find"])
    lead_seen = "\n".join(fake.seen["lead"])
    assert "example.com was best." in lead_seen
    assert "There is no agent `nobody`" in lead_seen


async def test_hard_stop_pauses_and_resume_continues(make_run):
    scripts = {
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("a"), task("b", ["a"])]}),
        "a": [[bash("echo step1 > notes.txt")], [bash("echo step2 >> notes.txt")],
              [call("store_write", collection="companies", records=[record("Acme")])],
              [call("report", summary="a is done")]],
        "b": worker(summary="b is done"),
    }
    # every call costs about US$ 0.01: the budget runs out while `a` is still working
    run, fake = make_run(scripts, output_tokens=20_000, run_budget=0.03, hard_stop_ratio=1.2)
    assert await orchestrator.execute(run) == "paused"
    assert run.budget.spent <= run.cfg.hard_stop + 1e-9
    assert run.db.task("a")["status"] in ("running", "reviewing") and run.db.task("b")["status"] == "pending"
    assert any("budget of US$ 0.03 is used up" in t for t in fake.seen.get("lead", [])) or run.budget.winding_down
    run.db.conn.close()

    # a new process, a bigger budget
    cfg = config.RunConfig(**(run.cfg.to_dict() | {"run_budget": 5.0}))
    probe = runmod.open_run(cfg, None)
    probe.db.set_meta("config", cfg.to_dict())
    probe.db.conn.close()
    fake2 = FakeLLM(scripts, output_tokens=200)
    run2 = runmod.open_run(cfg, fake2)
    assert await orchestrator.execute(run2) == "done"
    assert orchestrator.summary(run2)["tasks"] == {"a": "done", "b": "done"}
    assert (run2.cfg.run_dir / "work" / "a" / "notes.txt").read_text() == "step1\nstep2\n"
    assert [r["name"] for r in stored(run2)] == ["Acme"]
    assert any("interrupted and has resumed" in t for t in fake2.seen["lead"])
    run2.db.conn.close()


async def test_wind_down_drops_what_never_started(make_run):
    plan_input = GOAL | {"tasks": [task("a"), task("b", ["a"])]}
    run, fake = make_run({"lead": lead_script(plan_input), "a": worker()},
                         output_tokens=20_000, run_budget=0.035, hard_stop_ratio=4.0)
    assert await orchestrator.execute(run) in ("done", "done_unverified")
    assert run.db.task("b")["status"] == "cancelled" and "b" not in fake.seen
    assert any("is used up" in t and "stops for good at US$ 0.14" in t for t in fake.seen["lead"])


async def test_lead_that_does_nothing_stalls(make_run):
    run, fake = make_run({"lead": ["I will think about it."]})
    assert await orchestrator.execute(run) == "stalled"
    assert sum("Nothing is running and you have not finished" in t for t in fake.seen["lead"]) == 3


async def test_silent_worker_is_nudged_then_fails(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("a")]}),
        "a": ["All good!", "Really, all good.", "Done."],
    })
    assert await orchestrator.execute(run) == "done"
    assert run.db.task("a")["status"] == "failed" and "stopped without handing in" in run.db.task("a")["findings"]
    assert sum("ended your turn without handing in" in t for t in fake.seen["a"]) == 2


async def test_compaction_keeps_the_agent_going(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("a")]}),
        "a": lambda messages: [call("report", summary="after compaction")]
        if any(b.get("type") == "compaction" for m in messages for b in m["content"]) else [bash("echo big")],
    })
    fake.big.add("a")
    assert await orchestrator.execute(run) == "done"
    assert fake.compactions == 1 and run.db.task("a")["summary"] == "after compaction"
    assert any("You are `a`, a worker." in t and "compacted" in t for t in fake.seen["a"])


async def test_final_review_can_send_the_lead_back(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("a")]}),
        "a": worker(),
        "final.review1": [[call("verdict", passed=False, findings="The deliverable names no source at all.")]],
    })
    assert await orchestrator.execute(run) == "done"
    assert run.db.get_meta("final_reviews") == 2
    assert any("names no source" in t for t in fake.seen["lead"])


async def test_unverified_when_final_review_never_passes(make_run):
    no = [[call("verdict", passed=False, findings="The deliverable names no source at all.")]]
    run, _ = make_run({"lead": lead_script(GOAL | {"tasks": [task("a")]}), "a": worker(),
                       "final.review1": no, "final.review2": no})
    assert await orchestrator.execute(run) == "done_unverified"
    assert "names no source" in orchestrator.summary(run)["final_findings"]


def test_no_environment_leaks_into_the_prompt():
    from taskswarm import prompts

    text = prompts.system_prompt(config.RunConfig(run_id="x"))
    assert "{" not in text and os.environ.get("USER", "nobody-here") not in text
    assert "stops for good at US$ 2.40" in text
