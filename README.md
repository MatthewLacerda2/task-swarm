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
                                      │     │   ▲                            │  │
                                      │     │   └────── findings ◄── fail ───┘  └─ partial ──> the task's owner
                                      │     │        (wrong work, up to 3 times)    decides: accept, or send back
                                      │     └─ may split its task once into sub-tasks it owns
                                      └─ a task starts when everything it is blocked by is done
```

- **A stronger model drafts the plan.** A good plan lifts every worker under it, so
  by default the first draft is written by Claude Opus 5.5 in one or two calls, and
  the lead and the rest of the team run on Claude Haiku 5.5. The planner is skipped
  when the budget is too small for it (under about US$ 1.10), or with
  `--planner none`; then the lead plans.
- **You choose who runs on what, before the run:** `--planner`, `--lead` and
  `--worker` take `haiku`, `sonnet`, `opus` or `fable`. Price it by how much the task
  matters and how hard it is. One rule is enforced: no one hands work to a stronger
  model than its own, so worker <= lead <= planner; a run that breaks it doesn't
  start. A reviewer runs on the model of whoever's work it checks. The per-agent caps
  grow with `--budget`, and a run whose caps can't cover its models is refused with
  the numbers to set.
- **The lead owns the plan and puts the result together; it doesn't do the pieces.**
  The plan sets the goal, how the finished work will be checked (`done_when`), the
  structure of the store, and the tasks. Each task has a brief that stands alone and
  a `verify` that says how to check it. The plan can change at any time.
- **Whoever holds a task decides how to do it.** Tasks hand out a result to reach,
  not steps to follow. A worker goes back to whoever gave it the task for two things
  only: something it can't do the task without, or a premise in its brief that turned
  out to be false.
- **A worker may contest a premise, never override it.** One task has one owner (the
  lead for its tasks, a worker for the sub-tasks it made), and only the owner changes
  what the task is. The owner's answer is recorded on the task as a decision, and the
  reviewer checks the work against the brief plus those decisions: work that departs
  from the brief on the worker's own judgment doesn't pass.
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
  and re-does the checks: opens files, re-fetches sources. The finished deliverable
  gets the same treatment against `done_when`.
- **A review has three results, and each goes to whoever can act on it.** `pass`
  closes the task. `fail` (something is wrong, or missing and within reach) goes back
  to the worker with the findings. `partial` (what was delivered is right, something
  asked for is missing, and trying again would not get it) goes to the task's owner,
  who settles it with `decide`: accept the task as delivered, or send it back with
  what to do instead. Nothing blocked by the task starts until then. Only `fail`
  counts against the task. A worker says what it could not deliver and why in its
  report (`not_done`), and the reviewer checks that reason before accepting it.
  What an owner accepted as missing stays on the task and is shown to the tasks that
  build on it, to the final reviewer and in the run's README.
- **A checkpoint stops the plan when what it rests on fails.** A task that tests
  something later tasks are built on (that a quantity can be estimated, that a source
  has what is needed) can be marked `checkpoint`, with the question it answers. Its
  reviewer gives two answers: a verdict on the work, and `yes`, `no` or `unclear` on
  the question. Whatever the answer, nothing blocked by the task starts until its
  owner has read the result and ruled with `decide`, and the ruling is recorded and
  shown to the tasks after it and to the final reviewer. Without it a plan is a
  straight line: in one real run an estimate failed its own test and the team built
  two ranked lists on it anyway.
- **A reference figure is a check, never a target.** Workers are told not to scale,
  shift or filter a result until it agrees with a known figure, and reviewers not to
  pass work that did. Planners are told to give such a figure as something to
  compare with, not as the answer to expect.
- **A reviewer checks its own findings.** The first review of a task, and of the
  deliverable, is a full one. When the work is handed in again, the same reviewer
  checks that each finding was fixed instead of a new reviewer starting over with a
  new list (up to three reviews). A new reviewer takes over, with the earlier
  findings, only when the first can't continue. A deliverable that is right in what
  it says but could not be made complete ends the run as `done_with_gaps`, with what
  is missing listed.
- **The store is structured before the work starts.** The lead declares collections
  and fields in the plan; every write is checked (`taskswarm/store.py`). A record
  with an undeclared field, a wrong type or no `source` and `retrieved` date is
  refused with the reason. Two records with the same key and different values are a
  conflict: the first is kept and both are logged as a signal.
- **A stored record can be corrected or removed** (`store_update`, `store_delete`), by
  the agent that wrote it or by the owner of its task; the lead can change any. The
  corrected record passes the same checks, a change is applied whole or not at all,
  and what was done to which records, by whom and why is logged in `changes.jsonl`.
  The old content is not kept, so a record that should never have been stored is
  really gone.
- **Messages are direct and by name** (`send_message`), like Claude Code's
  SendMessage: to the lead or to a task's worker, and an agent that already handed
  in is woken with its memory intact to answer. Results don't travel by message;
  they go in reports and in the store.
- **Channels carry tips between agents** (`post`, `taskswarm/channels.py`), like
  Slack channels: each has a name by theme and a creation time, each post says who
  wrote it and when, and a post is at most 1,000 characters. A worker that finds out
  a site limits requests says so once, and the others are shown it with the result
  of their next command. A post wakes nobody and changes no task. The store is for
  what was found; a channel is for what helps the work along.
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
- **Limits are announced.** The lead and the workers at work are told when 80% of the
  run budget is used, then at every 5%, then at every 1% from 95%, and asked to put
  what is left where it is worth most. An agent gets the same notices about its own
  calls for one assignment (150 by default); at the limit it is stopped at once.
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
uv run task-swarm run --budget 5 --lead sonnet --worker haiku --data ~/datasets "..."
uv run task-swarm status <run>
uv run task-swarm transcript <run> lead        # or a task id, <task>.review1, final.review1
uv run task-swarm resume <run> --budget 2      # after an interruption, or to add money
uv run task-swarm list

uv run task-swarm plan <run> > plan.json       # the plan a run started from
uv run task-swarm run --plan plan.json "..."   # start from your own plan; no planner is called
```

A run's exit code is 0 when the final review passed (`done`) or found the result right but
incomplete (`done_with_gaps`); anything else is 1.

**Bringing your own plan.** `--plan` takes a JSON file with what the `plan` tool takes:
`goal`, `done_when`, `collections` and `tasks` (each with `id`, `title`, `brief`,
`verify`, `blocked_by`). It is checked like any plan; if a check fails the run does not
start and every problem is listed. The lead owns it from the first minute and can
still change it. This is the cheapest way to a good run: think the task through
beforehand, with a model or without, and hand in the result.

## Where results go

Not in this repository. Each run has a folder under `~/.local/share/task-swarm/runs/`
(change it with `runs_root` in `taskswarm/config.local.toml` or `TASK_SWARM_RUNS`):

```
<run>/README.md        what the run was and where everything is, written by the tool
<run>/deliverable/     the file the lead handed in
<run>/store/           <collection>.jsonl, schema.json, signals.jsonl, changes.jsonl
<run>/channels/        <channel>.jsonl: what agents told each other
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

- **Checkpoints are tested with a scripted model only.** No real run has used one
  yet. A checkpoint forces a recorded decision; it cannot force a good one.
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
  Opus 5.5 and Fable 5.2. Run for real so far: Haiku workers, a Haiku or Sonnet lead,
  an Opus planner. Opus or Fable as lead or worker, and Fable as planner (which
  needs a run budget of about US$ 2.20 or more), are covered by scripted tests only.
- **Waiting inside a command is refused, but only the plain case.** In one run a
  Sonnet lead slept two minutes inside a command instead of ending its turn. Now a
  command that sleeps 15 seconds or more is not run while its agent has tasks open.
  A loop of short sleeps still gets through.
- **What goes in a channel is advice, not a check.** The tool checks a post's size
  and the channel's name; nothing stops an agent from posting a finding there
  instead of in the store.

## Safety

Agents get real internet access and run real commands on your machine, inside the
sandbox. Run the escape tests before the first run. What agents do on the internet
leaves from your IP. Web pages can contain text written to mislead an agent: the
sandbox keeps the damage inside the run's folder, and reviewers re-check sources, but
treat a deliverable as checked work, not as proof. Agents can't reach your local
network (`block_lan`, on by default; `--set block_lan=false` turns it off). A folder
you pass with `--data` is mounted read-only for every agent at `/task/data`, so a
large download you already have happens once. If that folder has a `README.md` at its
top, every agent is given it (up to 8,000 characters) in its instructions, so say there
what the folder holds and where.

## License

MIT
