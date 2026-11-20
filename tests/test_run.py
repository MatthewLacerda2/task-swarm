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

    # the run's folder says what it is and where everything in it is
    readme = (run.cfg.run_dir / "README.md").read_text()
    assert "Status: done: the final review passed" in readme and "Find things." in readme
    assert GOAL["goal"] in readme and GOAL["done_when"] in readme
    assert "| `deliverable/answer.md` |" in readme and "| `store/companies.jsonl` | 1 records" in readme
    assert "`employees` (integer, min 0)" in readme and "key: name" in readme
    assert "- `find` (done): Task find\n  Found Acme; one had no source." in readme
    assert "One maker found." in readme and "## What the final reviewer found" in readme


async def test_failed_review_goes_back_to_the_worker(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find")]}),
        "find": [
            [call("report", summary="Done, I think.")],
            [call("store_write", collection="companies", records=[record("Acme")])],
            [call("report", summary="Now stored Acme.")],
        ],
        "find.review1": [[call("verdict", result="fail", findings="The store is empty: nothing was written.")],
                         [call("verdict", result="pass", findings="Fixed: Acme is stored, and its page names it.")]],
    })
    assert await orchestrator.execute(run) == "done"
    t = run.db.task("find")
    assert t["status"] == "done" and t["attempts"] == 2 and t["summary"] == "Now stored Acme."
    assert any("The store is empty" in x and "failed review 1 of at most 3" in x for x in fake.seen["find"])
    # the reviewer that made the finding checks the fix; no second reviewer starts over
    assert "find.review2" not in fake.seen and (t["reviewer"], t["fails"]) == ("find.review1", 1)
    again = fake.seen["find.review1"][1]
    assert "handed in again" in again and "review 2 of this task" in again and "Now stored Acme." in again
    assert "don't start the review over" in again


async def test_task_that_never_passes_fails_and_the_lead_replans(make_run):
    plan_input = GOAL | {"collections": COLLECTIONS, "tasks": [task("find"), task("check", ["find"])]}

    def react(last):
        if "Task `find` (Task find) failed" in last:
            assert "Can never start because of a failed task: check" in last
            return [call("plan", cancel=["check"], tasks=[task("find2")])]

    no = [[call("verdict", result="fail", findings="Still nothing stored, as before.")]]
    run, fake = make_run({
        "lead": lead_script(plan_input, react=react),
        "find": [[call("report", summary=f"Try {n}.")] for n in range(1, 5)],
        "find.review1": no * 2,
        "find2": worker(record("Acme")),
    }, max_attempts=2)
    assert await orchestrator.execute(run) == "done"
    assert run.db.task("find")["fails"] == 2 and "find.review2" not in fake.seen
    assert orchestrator.summary(run)["tasks"] == {"find": "failed", "check": "cancelled", "find2": "done"}
    assert "check" not in fake.seen


async def test_roles_are_enforced(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("find")]}),
        "find": [
            [call("plan", goal="mine now", done_when="never", tasks=[])],
            [call("verdict", result="pass", findings="I pass myself, it is all fine.")],
            [call("finish", path="x", summary="done")],
            [call("report", summary="Did my own task.")],
        ],
        "find.review1": [
            [call("report", summary="I am the worker now.")],
            [call("store_write", collection="companies", records=[])],
            [call("verdict", result="pass", findings="Checked the report against the brief.")],
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
        "final.review1": [[call("verdict", result="fail", findings="The deliverable names no source at all.")],
                          [call("verdict", result="pass", findings="It names the source of each claim now.")]],
    })
    assert await orchestrator.execute(run) == "done"
    assert run.db.get_meta("final_reviews") == 2 and "final.review2" not in fake.seen
    assert any("names no source" in t for t in fake.seen["lead"])


async def test_unverified_when_final_review_never_passes(make_run):
    no = [call("verdict", result="fail", findings="The deliverable names no source at all.")]
    run, _ = make_run({"lead": lead_script(GOAL | {"tasks": [task("a")]}), "a": worker(),
                       "final.review1": lambda messages: no})
    assert await orchestrator.execute(run) == "done_unverified"
    assert run.db.get_meta("final_reviews") == 3
    assert "names no source" in orchestrator.summary(run)["final_findings"]


def test_no_environment_leaks_into_the_prompt():
    from taskswarm import prompts

    text = prompts.system_prompt(config.RunConfig(run_id="x"))
    assert "{" not in text and os.environ.get("USER", "nobody-here") not in text
    assert "stops for good at US$ 2.40" in text


async def test_a_worker_corrects_its_records_after_a_failed_review(make_run):
    """What went wrong in the first real run: wrong records could only be added to, never fixed."""
    def reviewer(messages):
        rows = [r for r in stored(run) if r["_task"] == "find"]
        ok = [r["name"] for r in rows] == ["Acme"] and rows[0].get("employees") == 15
        return [call("verdict", result="pass" if ok else "fail", findings="Read the store: Bolt is a reseller and Acme has 15 employees.")]

    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find"), task("other", ["find"])]}),
        "find": [
            [call("store_write", collection="companies", records=[record("Acme", employees=12), record("Bolt")])],
            [call("report", summary="Stored two makers.")],
            [call("store_delete", collection="companies", where={"name": "Bolt"}, reason="Bolt resells, it is no maker"),
             call("store_update", collection="companies", ids=[1], set={"employees": 15}, reason="the page says 15")],
            [call("report", summary="Removed the reseller and corrected Acme.")],
        ],
        "find.review1": reviewer,
        "other": [
            [call("store_write", collection="companies", records=[record("Coil")])],
            [call("store_delete", collection="companies", where={"name": "Acme"}, reason="not mine, but I try"),
             call("store_update", collection="companies", ids=[3], set={"kind": "maker"}),
             call("store_update", collection="companies", ids=[3], where={"name": "Coil"}, set={"kind": "maker"},
                  reason="both ways of picking"),
             call("store_update", collection="companies", ids=[3], set={"kind": "maker"}, reason="it makes them")],
            [call("report", summary="Stored Coil.")],
        ],
    })
    assert await orchestrator.execute(run) == "done"
    assert {r["name"]: r.get("employees") for r in stored(run)} == {"Acme": 15, "Coil": None}
    find, other = "\n".join(fake.seen["find"]), "\n".join(fake.seen["other"])
    assert "companies: 1 records deleted" in find and "companies: 1 records updated" in find
    assert run.db.task("find")["attempts"] == 2 and run.store.changes == 3
    # another task's records are not yours to change; a change needs a reason and one way of picking
    assert "matching records were written under other tasks (find)" in other
    assert "`reason` is missing" in other and "either `ids` or `where`, not both" in other
    # the final reviewer is told corrections were made, and the run's README points at the log
    assert "Corrections logged (records updated or deleted after they were stored): 3" in fake.seen["final.review1"][0]
    assert "| `store/changes.jsonl` | 3 corrections" in (run.cfg.run_dir / "README.md").read_text()


async def test_who_may_change_which_records(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find")]}, react=lambda last: (
            [call("store_delete", collection="companies", where={"_task": "find"}, reason="the lead may remove any"),
             call("send_message", to="find", message="I removed your record; anything to add?")]
            if "No task is running" in last and len(stored(run)) == 1
            else [bash(DELIVER)] if "Sent to `find`" in last else None)),
        "find": [
            [call("store_write", collection="companies", records=[record("Acme")])],
            [call("report", summary="Stored Acme.")],
            # woken after handing in: its records are no longer its to change
            [call("store_update", collection="companies", ids=[1], set={"employees": 1}, reason="a late thought")],
        ],
        "find.review1": [[call("store_delete", collection="companies", ids=[1], reason="a reviewer fixing things")],
                         [call("verdict", result="pass", findings="Opened the record and its source.")]],
    })
    assert await orchestrator.execute(run) == "done"
    assert stored(run) == []
    assert "`store_delete` is not for your role (reviewer)" in "\n".join(fake.seen["find.review1"])
    assert "Your task is handed in" in "\n".join(fake.seen["find"])


async def test_agent_is_warned_as_its_calls_run_down_and_stopped_at_the_limit(make_run):
    def heeds(messages):
        if "Hand in now" in texts_of(messages[-1]):
            return [call("report", summary="What I had by then.")]
        return [bash("true")]

    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("a"), task("b")]}),
        "a": heeds,
        "b": lambda messages: [bash("true")],
    }, max_calls=20)
    assert await orchestrator.execute(run) == "done"
    # told at 80%, then every 5%, then every 1% from 95%: with 20 calls, after calls 16, 17, 18 and 19
    for name in ("a", "b"):
        seen = fake.seen[name]
        assert sum("You have used 16 of the 20 model calls" in t and "worth most" in t for t in seen) == 1
        assert sum("17 of your 20 calls are used, 3 calls left" in t for t in seen) == 1
        assert sum("18 of your 20 calls are used" in t for t in seen) == 1
        assert sum("1 call left of 20. Hand in now: call `report`" in t for t in seen) == 1
        assert not any("15 of" in t for t in seen) and fake.calls.count(name) == 20
    # the one that handed in on its last call is done; the one that went on is stopped, with no call 21
    assert run.db.task("a")["status"] == "done" and run.db.task("a")["summary"] == "What I had by then."
    assert run.db.task("b")["status"] == "failed" and "used all its calls" in run.db.task("b")["findings"]


def test_notice_marks():
    assert config.NOTICES == (80, 85, 90, 95, 96, 97, 98, 99, 100)
    assert config.notice_mark(0, 79.9) is None and config.notice_mark(79.9, 80) == 80
    assert config.notice_mark(80, 84.9) is None and config.notice_mark(80, 85) == 85
    assert config.notice_mark(94, 97.5) == 97          # several passed at once: one notice, the highest
    assert config.notice_mark(99, 130) == 100 and config.notice_mark(100, 130) is None


async def test_budget_running_down_is_told_to_the_lead_and_the_workers_not_the_reviewers(make_run):
    def a(messages):
        if "is used up" in texts_of(messages[-1]):
            return [call("report", summary="The part that mattered most.")]
        return [bash("true")]

    # every call costs about a tenth of the budget
    run, fake = make_run({"lead": lead_script(GOAL | {"tasks": [task("a")]}), "a": a},
                         output_tokens=20_000, run_budget=0.10, hard_stop_ratio=4.0)
    assert await orchestrator.execute(run) in ("done", "done_unverified")
    worker_saw, lead_saw = fake.seen["a"], fake.seen["lead"]
    assert any("Run budget: 80% used (US$ 0.08 of the run's budget of US$ 0.10)" in t
               and "worth most" in t and "even if a smaller part is closer to done" in t for t in worker_saw)
    assert any("Run budget: 90% used" in t for t in worker_saw)
    assert any("budget of US$ 0.10 is used up" in t and "Hand in now: call `report`" in t for t in worker_saw)
    assert any("Run budget: 80% used" in t and "cancel the tasks that matter least" in t for t in lead_saw)
    assert any("is used up" in t and "stops for good at US$ 0.40" in t for t in lead_saw)
    assert not any("Run budget" in t or "used up" in t for t in fake.seen["a.review1"])
    assert run.db.task("a")["summary"] == "The part that mattered most."

