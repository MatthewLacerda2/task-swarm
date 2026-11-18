"""Who runs on what, who may hand work to whom, and who may change a task."""

from __future__ import annotations

import pytest

from conftest import GOAL, call, lead_script, task, texts_of, tool_calls, worker
from taskswarm import config, orchestrator, prompts
from taskswarm.__main__ import main

HAIKU, SONNET, OPUS, FABLE = config.RANK


def test_names():
    assert config.resolve_model("Opus") == OPUS and config.resolve_model("none") == ""
    assert config.resolve_model("claude-sonnet-5-5") == SONNET


def test_defaults_are_opus_plans_haiku_works():
    cfg = config.RunConfig(run_id="x")
    assert (cfg.plan_model, cfg.lead_model, cfg.worker_model) == (OPUS, HAIKU, HAIKU)
    assert cfg.problems() == [] and cfg.block_lan


@pytest.mark.parametrize("planner, lead, worker, needle", [
    (OPUS, HAIKU, SONNET, "a claude-haiku-5-5 lead can't hand work to claude-sonnet-5-5 workers"),
    (SONNET, OPUS, HAIKU, "a claude-sonnet-5-5 planner can't hand its plan to a claude-opus-5-5 lead"),
    ("", HAIKU, OPUS, "can't hand work to claude-opus-5-5 workers"),
    (OPUS, "gpt-9", HAIKU, "the lead's model 'gpt-9' has no prices"),
])
def test_no_one_hands_work_to_a_stronger_model(planner, lead, worker, needle):
    cfg = config.RunConfig(run_id="x", plan_model=planner, lead_model=lead, worker_model=worker,
                           lead_budget=50, agent_budget=50)
    assert any(needle in p for p in cfg.problems()), cfg.problems()


@pytest.mark.parametrize("planner, lead, worker", [
    (OPUS, HAIKU, HAIKU), (OPUS, OPUS, HAIKU), (FABLE, OPUS, SONNET), ("", SONNET, SONNET), (OPUS, OPUS, OPUS),
])
def test_equal_or_below_is_fine(planner, lead, worker):
    cfg = config.RunConfig(run_id="x", plan_model=planner, lead_model=lead, worker_model=worker,
                           lead_budget=50, agent_budget=50)
    assert cfg.problems() == []


def test_caps_must_fit_the_models_and_grow_with_the_budget():
    cfg = config.RunConfig(run_id="x", worker_model=OPUS, lead_model=OPUS)
    problems = "\n".join(cfg.problems())
    assert "agent_budget is US$ 0.40, too little for a claude-opus-5-5 worker" in problems
    assert "lead_budget is US$ 0.60" in problems and "raise --budget" in problems
    cfg.scale_caps(10 / cfg.run_budget)
    cfg.run_budget = 10
    assert (cfg.lead_budget, cfg.agent_budget, cfg.task_budget, cfg.planner_budget) == (3.0, 2.0, 5.0, 3.0)
    assert cfg.problems() == []


def test_a_run_that_breaks_the_rule_does_not_start(make_run):
    with pytest.raises(SystemExit) as e:
        make_run({}, lead_model=HAIKU, worker_model=OPUS, agent_budget=50)
    assert "This run can't start" in str(e.value) and "stronger model" in str(e.value)
    assert not (config.RUNS_ROOT / "t").exists()


def test_cli_refuses_before_spending(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(config, "read_api_key", lambda: "not-a-key")
    with pytest.raises(SystemExit) as e:
        main(["run", "--id", "r", "--lead", "haiku", "--worker", "sonnet", "do a thing"])
    assert "can't hand work to claude-sonnet-5-5 workers" in str(e.value)


def test_old_stored_configs_still_load():
    cfg = config.RunConfig.from_dict({"run_id": "old", "model": HAIKU, "run_budget": 0.6, "gone_setting": 1})
    assert cfg.worker_model == cfg.lead_model == HAIKU and cfg.run_budget == 0.6


def test_prompt_states_costs_for_the_workers_model():
    haiku = prompts.system_prompt(config.RunConfig(run_id="x"))
    opus = prompts.system_prompt(config.RunConfig(run_id="x", worker_model=OPUS, lead_model=OPUS))
    assert "roughly US$ 0.00035 to 0.0018" in haiku and "roughly US$ 0.013 to 0.064" in opus
    assert "The local network is not reachable." in haiku and "/task/data" not in haiku
    assert "/task/data (read-only)" in prompts.system_prompt(config.RunConfig(run_id="x", data_dir="/srv/d"))


async def test_each_role_runs_on_its_model(make_run):
    run, fake = make_run({
        "planner": [[call("plan", **(GOAL | {"tasks": [task("a")]}))]],
        "lead": lead_script(None),
        "a": worker(),
    }, plan_model=FABLE, lead_model=OPUS, worker_model=SONNET, run_budget=20, lead_budget=6, agent_budget=4,
        planner_budget=6, task_budget=10)
    assert await orchestrator.execute(run) == "done"
    assert fake.models == {"planner": FABLE, "lead": OPUS, "a": SONNET, "a.review1": SONNET, "final.review1": OPUS}
    # the planner thinks harder and keeps a short cache; the team keeps its cache for an hour
    assert fake.settings["planner"] == ("high", False) and fake.settings["a"] == ("high", True)
    # every call is billed at the prices of the model that made it
    rows = {r["agent"]: r["c"] for r in run.db.all("SELECT agent, SUM(cost) AS c FROM usage GROUP BY agent")}
    calls = {r["agent"]: r["n"] for r in run.db.all("SELECT agent, COUNT(*) AS n FROM usage GROUP BY agent")}
    for agent, model in fake.models.items():
        p = config.prices(model)
        assert rows[agent] == pytest.approx(calls[agent] * (500 * p.input + 2500 * p.cache_read + 200 * p.output) / 1e6)


async def test_planner_is_skipped_when_the_lead_is_the_same_model(make_run):
    run, fake = make_run({"lead": lead_script(GOAL | {"tasks": [task("a")]}), "a": worker()},
                         plan_model=OPUS, lead_model=OPUS, run_budget=20, lead_budget=6)
    assert await orchestrator.execute(run) == "done"
    assert "planner" not in fake.seen


# --- only a task's owner changes the task --------------------------------------------------


async def test_owner_decides_a_contested_premise_and_the_reviewer_sees_it(make_run):
    def react(last):
        if "[Message from `a`]" in last:
            return [call("send_message", to="a", message="You are right: use the 2025 list, not the 2024 one."),
                    call("send_message", to="b", message="Nothing changes for you.")]

    def contesting(messages):
        last = texts_of(messages[-1])
        if not tool_calls(messages):
            return [call("send_message", to="lead", message="The brief says 2024, but the 2024 list was withdrawn."),
                    call("send_message", to="b", message="Use the 2025 list, I say so.")]
        if "use the 2025 list" in last:
            return [call("report", summary="Done with the 2025 list, as the lead decided.")]
        return [call("bash", command="sleep 0.2; echo keeping to the brief meanwhile")]

    def slow(messages):
        return [call("bash", command="sleep 1; echo working")] if len(tool_calls(messages)) < 2 \
            else [call("report", summary="b is done")]

    run, fake = make_run({
        "lead": lead_script(GOAL | {"tasks": [task("a"), task("b")]}, react=react),
        "a": contesting, "b": slow,
    })
    assert await orchestrator.execute(run) == "done"
    # the owner's answer is on the task; a peer's opinion is not a decision on anyone's task
    assert "use the 2025 list" in run.db.task("a")["decisions"]
    assert "Nothing changes for you." in run.db.task("b")["decisions"] and "I say so" not in run.db.task("b")["decisions"]
    review = fake.seen["a.review1"][0]
    assert "# Decisions by the task's owner since the brief" in review and "use the 2025 list" in review
    assert "recorded on it as your decision" in "\n".join(fake.seen["lead"])
    assert "recorded" not in "\n".join(t for t in fake.seen["a"] if "Sent to `b`" in t)


def test_the_rule_is_in_what_agents_are_told():
    text = prompts.system_prompt(config.RunConfig(run_id="x"))
    assert "You may contest a premise; you may never override it." in text
    assert "Work that departs from it on the worker's own judgment doesn't pass" in text
