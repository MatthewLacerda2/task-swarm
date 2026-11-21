"""A checkpoint tests what later tasks rest on. Its reviewer answers the question, and whatever the
answer, nothing blocked by it starts until its owner has ruled."""

from __future__ import annotations

from conftest import COLLECTIONS, GOAL, call, lead_script, task, worker
from taskswarm import orchestrator, plan, status

QUESTION = "Can a maker's size be estimated from its website better than always guessing the most common size?"
NO = [[call("verdict", result="pass", findings="Re-ran the test: 4 of 39 right, a constant guess gets 23.")],
      [call("verdict", result="pass", holds="no", findings="Re-ran the test: 4 of 39 right, a constant guess gets 23.")]]
RULING = "Sizes can't be estimated, so `rank` is cancelled and `list` delivers the makers without a size."


def checkpoint_plan(*more):
    return GOAL | {"collections": COLLECTIONS,
                   "tasks": [task("test", checkpoint=QUESTION), task("rank", ["test"]), *more]}


async def test_a_checkpoint_holds_what_is_built_on_it_until_its_owner_rules(make_run):
    def react(last):
        if "Checkpoint `test` (Task test) was reviewed" in last:
            assert QUESTION in last and "The reviewer's answer: no." in last and "4 of 39 right" in last
            assert "Blocked by it: rank." in last and "Change the plan first" in last and "No task is running" not in last
            return [call("finish", path="answer.md", summary="too early"),
                    call("decide", task="test", accept=True),
                    call("plan", cancel=["rank"], tasks=[task("list", ["test"])]),
                    call("decide", task="test", accept=True, note=RULING)]

    run, fake = make_run({"lead": lead_script(checkpoint_plan(), react=react),
                          "test": worker(summary="The estimate is right for 4 of 39."), "test.review1": NO,
                          "rank": worker(summary="Ranked."), "list": worker(summary="Listed without sizes.")})
    assert await orchestrator.execute(run) == "done"

    t = run.db.task("test")
    assert t["status"] == "done" and t["holds"] == "no" and t["fails"] == 0 and RULING in t["decisions"]
    assert run.db.task("rank")["status"] == "cancelled" and "rank" not in fake.calls
    # the worker is told what rests on it, and the reviewer must answer the question
    assert "Your task is a checkpoint" in fake.seen["test"][0] and QUESTION in fake.seen["test"][0]
    review = "\n".join(fake.seen["test.review1"])
    assert "This task is a checkpoint" in review and "so `holds` is needed" in review
    # the owner could not finish around it, nor rule without saying why
    lead = "\n".join(fake.seen["lead"])
    assert "Tasks wait for your decision: test" in lead and "`test` is a checkpoint, so `note` is needed" in lead
    assert "`test` is done and your ruling is recorded" in lead
    # what is built on it, the final reviewer, the README and the status are told the answer and the ruling
    for seen in (fake.seen["list"][0], fake.seen["final.review1"][0]):
        assert QUESTION in seen and "answer: no" in seen and RULING in seen
    readme = (run.cfg.run_dir / "README.md").read_text()
    assert "Checkpoint: " + QUESTION in readme and "Reviewer's answer: no." in readme and RULING in readme
    assert "[checkpoint: no]" in status.render(run.cfg.run_id)


async def test_a_checkpoint_that_holds_still_waits_for_the_ruling(make_run):
    seen = []

    def react(last):
        if "Checkpoint `test`" in last:
            seen.append(run.db.task("rank")["status"])
            assert "The reviewer's answer: yes." in last and "If the plan still stands" in last
            return [call("decide", task="test", accept=True, note="The estimate beats the guess, the plan stands.")]

    run, fake = make_run({"lead": lead_script(checkpoint_plan(), react=react), "test": worker(),
                          "test.review1": [[call("verdict", result="pass", holds="yes", findings="Re-ran it: 23 of 39 right against 9 for a constant guess.")]],
                          "rank": worker(summary="Ranked.")})
    assert await orchestrator.execute(run) == "done"
    assert seen == ["pending"] and run.db.task("rank")["status"] == "done"
    assert fake.calls.index("rank") > fake.calls.index("test.review1")
    assert "the plan stands" in fake.seen["rank"][0]


async def test_the_owner_sends_a_checkpoint_back_with_another_method(make_run):
    note = "Test it on a random sample of makers, not on the ten best known."

    def react(last):
        if "Checkpoint `test`" in last and "answer: unclear" in last:
            return [call("decide", task="test", accept=False, note=note)]
        if "Checkpoint `test`" in last:
            return [call("decide", task="test", accept=True, note="On a random sample it holds; the plan stands.")]

    unclear = "The ten cases are the best-known makers."
    run, fake = make_run({
        "lead": lead_script(checkpoint_plan(), react=react),
        "test": [*worker(), [call("report", summary="On 60 random makers: 41 right against 22.")]],
        "test.review1": [[call("verdict", result="pass", holds="unclear", findings=unclear + " They prove little.")],
                         [call("verdict", result="pass", holds="yes", findings="Re-ran it on the random sample.")]],
        "rank": worker(summary="Ranked."),
    }, max_attempts=1)
    assert await orchestrator.execute(run) == "done"
    t = run.db.task("test")
    assert t["status"] == "done" and t["attempts"] == 2 and t["fails"] == 0 and t["holds"] == "yes"
    back = "\n".join(fake.seen["test"])
    assert "answer to the question was `unclear`" in back and unclear in back and note in back
    assert "give `holds` again" in fake.seen["test.review1"][1]


async def test_a_partial_checkpoint_is_settled_by_one_ruling(make_run):
    missing = "The brief asks for a test on 40 makers and only 12 publish their size."

    def react(last):
        if "Checkpoint `test`" in last:
            assert "It is also incomplete." in last and missing in last
            return [call("decide", task="test", accept=True),
                    call("plan", cancel=["rank"]),
                    call("decide", task="test", accept=True, note="Twelve cases are too few: `rank` is cancelled.")]

    run, fake = make_run({"lead": lead_script(checkpoint_plan(), react=react), "test": worker(),
                          "test.review1": [[call("verdict", result="partial", holds="unclear",
                                                 findings="The 12 cases are right.", missing=missing)]]})
    assert await orchestrator.execute(run) == "done"
    t = run.db.task("test")
    assert t["status"] == "done" and t["holds"] == "unclear" and missing in t["gaps"]
    assert "Twelve cases are too few" in t["decisions"] and "so `note` is needed" in "\n".join(fake.seen["lead"])


def test_a_checkpoint_must_say_what_rests_on_it(make_run):
    run, _ = make_run({})
    ok, text = plan.apply(run, GOAL | {"tasks": [task("test", checkpoint="yes")]})
    assert not ok and "`checkpoint` must say, as a question" in text
    ok, text = plan.apply(run, GOAL | {"tasks": [task("test", checkpoint=QUESTION), task("rank", ["test"])]})
    assert ok and "`test` pending: Task test [checkpoint]" in text
    assert run.db.task("test")["checkpoint"] == QUESTION and run.db.task("rank")["checkpoint"] is None
