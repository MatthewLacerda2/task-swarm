# task-swarm

Give it a task; a team of Claude agents, organized like a company, finishes it and
checks its own work. It is the "deep research" idea as a tool you run yourself: a
lead plans, workers each do one piece in parallel, a reviewer checks every piece, and
what the team finds goes into a structured store where every record names its source.

"task-swarm" is a working name.

## How it works

```
task ──> lead ──plan──> tasks (a dependency graph) ──> deliverable ──> final review
                          │
                          ├─ worker does one task ──report──> reviewer ──pass──> done
                          │         ▲                            │
                          │         └────── findings ◄── fail ───┘   (up to 3 reports)
                          └─ a task starts when everything it is blocked by is done
```

- **The lead plans and puts together; it doesn't do the pieces.** Its plan sets the
  goal, how the finished work will be checked (`done_when`), the structure of the
  store, and the tasks. Each task has a brief that stands alone and a `verify` that
  says how to check it. The plan can change at any time.
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

Measured with Claude Haiku 5.5: a five-product market survey (5 tasks, 12 agents, 35
sourced records, every piece reviewed against its sources) took 5 minutes and
US$ 0.05. Expect a team to use several times the tokens of one agent doing the same
task; what that buys is independent checking and parallel work.

## Limits today

- **One level of planning.** Only the lead splits work; a worker can't hand part of
  its task to a sub-team.
- **The tool checks a record's shape, agents check its truth.** `store_write` refuses
  a record without a source URL, but whether the page says what the record says is
  checked by reviewers, who sample. A sampled check can miss a wrong record.
- **A very small task isn't split.** The lead may do it alone; it still gets the
  final review.
- **Built for research, not for writing code together.** There is no shared
  repository with merges, as swarm had.
- **Only Claude Haiku 5.5 has prices** in `taskswarm/config.py`; another model needs
  its prices added before the budget can be trusted.

## Safety

Agents get real internet access and run real commands on your machine, inside the
sandbox. Run the escape tests before the first run. What agents do on the internet
leaves from your IP. Web pages can contain text written to mislead an agent: the
sandbox keeps the damage inside the run's folder, and reviewers re-check sources, but
treat a deliverable as checked work, not as proof. Agents can reach your local
network unless you set `block_lan=true`.

## License

MIT
