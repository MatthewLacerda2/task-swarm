# task-swarm

Give it a task; a team of Claude agents, organized like a company, finishes it and
checks its own work. It is the "deep research" idea as a tool you run yourself: a
lead plans, workers each do one piece in parallel, a reviewer checks every piece, and
what the team finds goes into a structured store where every record names its source.

"task-swarm" is a working name.

## How it works

```
task ──> planner ──plan──> lead ──> tasks (a dependency graph) ──> deliverable ──> final review
        (stronger model)              │
                                      ├─ worker does one task ──report──> reviewer ──pass──> done
                                      │     │   ▲                            │
                                      │     │   └────── findings ◄── fail ───┘   (up to 3 reports)
                                      │     └─ may split its task once into sub-tasks it owns
                                      └─ a task starts when everything it is blocked by is done
```

- **A stronger model drafts the plan.** A good plan lifts every worker under it, so
  the first draft is written by Claude Opus 5.5 (`plan_model`) in one or two calls;
  the lead and the rest of the team run on Claude Haiku 5.5. The planner is skipped
  when the budget is too small for it (under about US$ 1.10), or with
  `--set plan_model=`; then the lead plans.
- **The lead owns the plan and puts the result together; it doesn't do the pieces.**
  The plan sets the goal, how the finished work will be checked (`done_when`), the
  structure of the store, and the tasks. Each task has a brief that stands alone and
  a `verify` that says how to check it. The plan can change at any time.
- **Whoever holds a task decides how to do it.** Tasks hand out a result to reach,
  not steps to follow. A worker goes back to whoever gave it the task for two things
  only: something it can't do the task without, or a premise in its brief that turned
  out to be false.
- **A worker may split its own task into sub-tasks**, which it then owns: their
  reports come to it, and it still hands in the whole task. What keeps that from
  running away: a sub-task can't be split again; a task splits into two to six
  pieces; the sub-team spends its parent task's budget, not new money; only the lead
  sets the goal and the store's structure.
- **The dependency graph is the schedule.** A task starts when the tasks it is
  blocked by have passed review, and its worker is handed their reports. Everything
  else runs in parallel.
- **Workers return a summary, not their context.** The lead carries each task's
  report (at most 4,000 characters), never how it was done.
- **Nothing counts as done until someone else checked it.** Every report goes to a
  reviewer with a fresh memory, who sees the brief, what to verify and the report,
  and re-does the checks: opens files, re-fetches sources. Failed work goes back to
  the worker with the findings. The finished deliverable gets the same treatment
  against `done_when`.
- **The store is structured before the work starts.** The lead declares collections
  and fields in the plan; every write is checked (`taskswarm/store.py`). A record
  with an undeclared field, a wrong type or no `source` and `retrieved` date is
  refused with the reason. Two records with the same key and different values are a
  conflict: the first is kept and both are logged as a signal.
- **Messages are direct and by name** (`send_message`), like Claude Code's
  SendMessage: to the lead or to a task's worker, and an agent that already handed
  in is woken with its memory intact to answer. Results don't travel by message;
  they go in reports and in the store.
- **The plan's rules are checks, not advice** (`taskswarm/plan.py`): a task without a
  way to verify it, a brief too short to stand alone, a dependency on a task that
  doesn't exist or a circle of dependencies is refused, and the refusal names every
  problem.

Under it, reused from [swarm](https://github.com/MatthewLacerda2/swarm):

- **Sandbox** (`systemd-run` + `pasta` + `bwrap`): every agent has a terminal with
  internet access, sees the system read-only, and can write only in its own folder.
  The user's home and other drives are not mounted at all.
  `tests/test_sandbox_escape.py` tries to get out in 55 ways; all must fail.
- **Budget:** every model call reserves its worst-case cost first, so parallel agents
  can't overspend. At the run budget no new task starts and the lead is asked to
  deliver; at the hard stop (1.2 x) the run pauses and can be resumed with more.
  Each agent has its own cap, and so does each top-level task with everything under it.
- **Cost:** every agent shares one cached prompt, whatever its role, and
  conversations are compacted before they reach the 100k-token price tier.
- **Resumable:** all state is in SQLite and plain files, written before each next
  step. A run killed by a crash or a power cut continues with `task-swarm resume`.

## Requirements

Linux with user namespaces, `bwrap`, `pasta` (passt), `nft`, systemd (user session),
Python 3.14 and `uv`, an Anthropic API key. Docker for SearXNG, which is what the
agents' `search` command talks to.

## Quick start

```bash
uv sync
cp services/searxng/settings.example.yml services/searxng/settings.yml   # set secret_key
(cd services/searxng && docker compose up -d)
cp deploy/task-swarm.slice ~/.config/systemd/user/ && systemctl --user daemon-reload

mkdir -p ~/.config/task-swarm && chmod 700 ~/.config/task-swarm
# put your API key (only the key) in ~/.config/task-swarm/anthropic.key, then:
chmod 600 ~/.config/task-swarm/anthropic.key

uv run pytest                                  # sandbox escape tests and the rest

uv run task-swarm run --budget 0.60 "Market survey of open-source vector databases: ..."
uv run task-swarm status <run>
uv run task-swarm transcript <run> lead        # or a task id, <task>.review1, final.review1
uv run task-swarm resume <run> --budget 2      # after an interruption, or to add money
uv run task-swarm list
```

A run's exit code is 0 only when the final review passed.

## Where results go

Not in this repository. Each run has a folder under `~/.local/share/task-swarm/runs/`
(change it with `runs_root` in `taskswarm/config.local.toml` or `TASK_SWARM_RUNS`):

```
<run>/deliverable/     the file the lead handed in
<run>/store/           <collection>.jsonl, schema.json, signals.jsonl
<run>/work/<agent>/    each agent's files
<run>/outputs/         full output of every command
<run>/run.db           the plan, tasks, conversations, costs
```

Machine-specific settings go in files named `*.local.*`, which git ignores (see
`taskswarm/config.local.example.toml`).

## What it costs

Measured on a five-product market survey (35 sourced records, every piece reviewed
against its sources):

| who planned | tasks | time | cost |
|---|---|---|---|
| the lead (Claude Haiku 5.5) | 5, one per product | 5 min | US$ 0.05 |
| the planner (Claude Opus 5.5) | 2, one per kind of source | 8 min | US$ 0.15, of which the plan is 0.09 |

On a task this small the plan costs more than the work. The planner earns its price
when the work under it is much larger than the plan. Expect a team to use several
times the tokens of one agent doing the same task; what that buys is independent
checking and parallel work.

## Limits today

- **Sub-teams are tested with a scripted model only.** In the real runs so far no
  worker chose to split its task.
- **A reviewer checks a record against its source, not how the source was read.** Two
  runs stored different entry prices for the same product, one from the page's hidden
  structured data and one from the visible page, and both passed review.
- **The tool checks a record's shape, agents check its truth.** `store_write` refuses
  a record without a source URL, but whether the page says what the record says is
  checked by reviewers, who sample. A sampled check can miss a wrong record.
- **A very small task isn't split.** The lead may do it alone; it still gets the
  final review.
- **Built for research, not for writing code together.** There is no shared
  repository with merges, as swarm had.
- **Models:** `taskswarm/config.py` has prices for Claude Haiku 5.5, Sonnet 5.5,
  Opus 5.5 and Fable 5.2. The team has only been run on Haiku 5.5 and the planner
  only on Opus 5.5. Fable 5.2 as planner needs `planner_budget=0.80` and a run
  budget of about US$ 2.20 or more.

## Safety

Agents get real internet access and run real commands on your machine, inside the
sandbox. Run the escape tests before the first run. What agents do on the internet
leaves from your IP. Web pages can contain text written to mislead an agent: the
sandbox keeps the damage inside the run's folder, and reviewers re-check sources, but
treat a deliverable as checked work, not as proof. Agents can reach your local
network unless you set `block_lan=true`.

## License

MIT
