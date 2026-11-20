"""The three results of a review and who acts on each: wrong work goes back to its worker,
incomplete work goes to the task's owner, and a reviewer checks its own findings."""

from __future__ import annotations

import sqlite3

from conftest import COLLECTIONS, GOAL, call, lead_script, record, task, texts_of, tool_calls, worker
from taskswarm import db as dbmod
from taskswarm import handlers, orchestrator, status

MISSING = "The brief asks for 80 makers and 41 are stored. I searched two more directories and found none new."
PARTIAL = [[call("verdict", result="partial", findings="Opened ten records and their sources; they are right.",
                 missing=MISSING)]]
SHORT = [call("store_write", collection="companies", records=[record("Acme")])], \
        [call("report", summary="Stored the 41 makers that exist.", not_done="80 were asked for; the directories list 41.")]


async def test_a_partial_task_waits_for_its_owner_who_accepts_it(make_run):
    def react(last):
        if "Task `find` (Task find) was reviewed" in last:
            assert "41 are stored" in last and "`decide`" in last and "No task is running" not in last
            return [call("finish", path="answer.md", summary="too early"),
                    call("decide", task="nope", accept=True),
                    call("decide", task="find", accept=True, note="41 makers are enough to compare prices.")]

    plan_input = GOAL | {"collections": COLLECTIONS, "tasks": [task("find"), task("check", ["find"])]}
    run, fake = make_run({"lead": lead_script(plan_input, react=react), "find": list(SHORT),
                          "find.review1": PARTIAL, "check": worker(summary="Checked the 41.")})
    assert await orchestrator.execute(run) == "done"

    t = run.db.task("find")
    assert t["status"] == "done" and t["attempts"] == 1 and t["fails"] == 0
    assert MISSING in t["gaps"] and "enough to compare prices" in t["gaps"]
    # the reviewer was shown what the worker said it did not deliver
    assert "Not delivered, and why:\n80 were asked for" in fake.seen["find.review1"][0]
    # nothing else moved until the owner decided, and it could not finish around the decision
    lead = "\n".join(fake.seen["lead"])
    assert "Tasks wait for your decision: find" in lead and "You have no task `nope`" in lead
    assert "`find` is done as delivered" in lead and "Still open: check" in lead
    # the task built on it, the final reviewer and the run's README are told what is missing
    assert "accepted it with this missing" in fake.seen["check"][0] and MISSING in fake.seen["check"][0]
    final = fake.seen["final.review1"][0]
    assert "Accepted by the lead with this missing" in final and "41 are stored" in final
    readme = (run.cfg.run_dir / "README.md").read_text()
    assert "- `find` (done, with gaps): Task find" in readme and "Asked for and not delivered: The brief asks" in readme
    assert "done* " in status.render(run.cfg.run_id)


async def test_the_owner_sends_a_partial_task_back_with_a_decision(make_run):
    note = "Aim for the 41 the directories list, and name the directories you read."

    def react(last):
        if "Task `find` (Task find) was reviewed" in last:
            return [call("decide", task="find", accept=False), call("decide", task="find", accept=False, note=note)]

    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find")]}, react=react),
        "find": [*SHORT, [call("report", summary="41 makers, from the two directories named in notes.md.")]],
        "find.review1": [*PARTIAL, [call("verdict", result="pass", findings="The 41 are there, directories named.")]],
    }, max_attempts=1)          # one failed review would end the task: a partial one is not a failed one
    assert await orchestrator.execute(run) == "done"

    t = run.db.task("find")
    assert t["status"] == "done" and t["attempts"] == 2 and t["fails"] == 0 and t["gaps"] is None
    assert "`note` is missing" in "\n".join(fake.seen["lead"])
    back = "\n".join(fake.seen["find"])
    assert MISSING in back and note in back and "did not count as a failed review" in back
    # the same reviewer then checks the work against the brief as the owner changed it
    second = fake.seen["find.review1"][1]
    assert "Decisions by the task's owner" in second and note in second and "earlier review found missing" in second


async def test_only_wrong_work_goes_back_to_the_worker(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find")]}),
        "find": [[call("report", summary="Done, I think.")], *worker(record("Acme"))],
        "find.review1": [
            [call("verdict", result="partial", findings="The store is empty: nothing was written.")],
            [call("verdict", result="pass", findings="The store is empty: nothing was written.", missing=MISSING)],
            [call("verdict", passed=False, findings="The store is empty: nothing was written.")],
            [call("verdict", result="fail", findings="The store is empty: nothing was written.")],
        ],
    })
    assert await orchestrator.execute(run) == "done"
    seen = "\n".join(fake.seen["find.review1"])
    assert "`missing` is empty" in seen and "`missing` goes with `partial` only" in seen
    assert "`result` must be `pass`, `partial` or `fail`" in seen
    t = run.db.task("find")
    assert t["status"] == "done" and t["attempts"] == 2 and t["fails"] == 1
    assert any("failed review 1 of at most 3" in x and "`not_done`" in x for x in fake.seen["find"])


async def test_a_worker_settles_the_partial_piece_of_its_own_task(make_run):
    def big(messages):
        last, said = texts_of(messages[-1]), tool_calls(messages)
        if not said:
            return [call("plan", tasks=[task("x"), task("y", ["x"])])]
        if "Task `big.x` (Task x) was reviewed" in last:
            return [call("report", summary="Too early."), call("decide", task="x", accept=True)]
        if "None of your sub-tasks" in last:
            return [call("report", summary="The whole, with what x could not get.")]
        return "Waiting for my sub-team."

    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("big")]}),
        "big": big, "big.x": list(SHORT), "big.x.review1": PARTIAL, "big.y": worker(summary="y is done"),
    })
    assert await orchestrator.execute(run) == "done"
    assert orchestrator.summary(run)["tasks"] == {"big": "done", "big.x": "done", "big.y": "done"}
    seen = "\n".join(fake.seen["big"])
    assert "Waiting for your decision (`decide`): big.x" in seen and "`big.x` is done as delivered" in seen
    # the piece built on it and the reviewer of the whole are told; the lead never hears of the piece
    assert MISSING in fake.seen["big.y"][0]
    assert "accepted by the worker with something missing" in fake.seen["big.review1"][0]
    assert "big.x" not in "\n".join(fake.seen["lead"])
    # a piece is settled by the worker that made it, nobody else
    text, is_error, _ = await handlers.handle(run, "lead", False, "decide", {"task": "big.x", "accept": True})
    assert is_error and "You have no task `big.x`" in text


async def test_a_reviewer_that_cannot_go_on_is_replaced(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"collections": COLLECTIONS, "tasks": [task("find")]}),
        "find": [[call("report", summary="Done, I think.")], *worker(record("Acme"), summary="Now stored Acme.")],
        # after its first verdict it never answers again
        "find.review1": [[call("verdict", result="fail", findings="The store is empty: nothing was written.")]],
    })
    assert await orchestrator.execute(run) == "done"
    first = fake.seen["find.review2"][0]
    assert "What the previous reviewer found" in first and "The store is empty" in first
    assert "review 2 of this task" in first and "Now stored Acme." in first
    t = run.db.task("find")
    assert (t["status"], t["reviewer"], t["asked"]) == ("done", "find.review2", 2)


async def test_the_final_reviewer_checks_its_own_findings(make_run):
    no = call("verdict", result="fail", findings="The deliverable names no source at all.")
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("a")]}), "a": worker(),
        "final.review1": [[no], [no], [call("verdict", result="pass", findings="Both findings are fixed now.")]],
    })
    assert await orchestrator.execute(run) == "done"
    assert run.db.get_meta("final_reviews") == 3
    assert [a["id"] for a in run.db.agents() if a["id"].startswith("final.")] == ["final.review1"]
    again = fake.seen["final.review1"][1]
    assert "handed in the deliverable again" in again and "review 2 of at most 3" in again
    assert "don't start the review over" in again and "`a` (done)" in again
    assert any("The same reviewer then checks" in t for t in fake.seen["lead"])


async def test_a_final_reviewer_that_cannot_go_on_is_replaced(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("a")]}), "a": worker(),
        # after its first verdict it never answers again
        "final.review1": [[call("verdict", result="fail", findings="The deliverable names no source at all.")]],
    })
    assert await orchestrator.execute(run) == "done"
    first = fake.seen["final.review2"][0]
    assert "What the previous final reviewer found" in first and "names no source at all" in first
    assert run.db.get_meta("final_reviewer") == "final.review2"


async def test_a_right_but_incomplete_deliverable_ends_the_run_with_gaps(make_run):
    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("a")]}), "a": worker(),
        "final.review1": [[call("verdict", result="partial", findings="Every claim traces to a stored record.",
                                missing="Done-when asks for every maker; the report says plainly that it has 41.")]],
    })
    assert await orchestrator.execute(run) == "done_with_gaps"
    assert await orchestrator.execute(run) == "done_with_gaps"          # it is finished: nothing runs again
    assert "Asked for and not delivered:\nDone-when asks" in orchestrator.summary(run)["final_findings"]
    assert "Status: done, with gaps" in (run.cfg.run_dir / "README.md").read_text()
    assert run.db.get_meta("final_reviews") == 1


def test_runs_made_before_partial_reviews_keep_their_count(tmp_path):
    path = tmp_path / "run.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, parent TEXT, seq INTEGER NOT NULL, title TEXT NOT NULL,"
                " brief TEXT NOT NULL, verify TEXT NOT NULL, blocked_by TEXT NOT NULL, status TEXT NOT NULL,"
                " attempts INTEGER NOT NULL DEFAULT 0, summary TEXT, how TEXT, findings TEXT, decisions TEXT,"
                " created REAL NOT NULL, updated REAL NOT NULL)")
    for n, (tid, state, attempts) in enumerate([("a", "done", 2), ("b", "running", 2), ("c", "reviewing", 1),
                                                ("d", "failed", 3), ("e", "pending", 0)]):
        old.execute("INSERT INTO tasks VALUES(?, NULL, ?, 't', 'b', 'v', '[]', ?, ?, NULL, NULL, NULL, NULL, 0, 0)",
                    (tid, n, state, attempts))
    old.commit()
    old.close()
    db = dbmod.DB(path)
    assert {t["id"]: t["fails"] for t in db.tasks()} == {"a": 1, "b": 2, "c": 0, "d": 3, "e": 0}
    assert all(t["gaps"] is None for t in db.tasks())
    # a review under way keeps the reviewer it had, and is not asked a second time
    assert {t["id"]: (t["reviewer"], t["asked"]) for t in db.tasks()}["c"] == (None, 1)
    db.conn.close()
