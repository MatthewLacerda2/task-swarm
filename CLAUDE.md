# CLAUDE.md

**task-swarm** runs a team of Claude agents on one task: a lead plans, workers do the
pieces, reviewers check them, findings go into a structured store. README.md says
what it does and how to run it.

## Where things are

- `taskswarm/orchestrator.py`: the schedule and the hand-offs (planner, work, review,
  rework, sub-teams, final review). No judgment lives here.
- `taskswarm/agent.py`: one agent's loop of model calls and tools.
- `taskswarm/plan.py`, `taskswarm/store.py`: the gates on the plan and on store writes.
- `taskswarm/tools.py`, `taskswarm/handlers.py`: the tools and what each does.
- `taskswarm/prompts/system.md`: what every agent is told.
- `taskswarm/sandbox.py`, `taskswarm/safeio.py`: what keeps agents inside their folder.

## Rules

- **A rule a check can hold is a check.** What the lead or a worker must do is
  enforced in `plan.py`, `store.py` or a handler, with a message that says what is
  wrong and what to do. The prompt explains; it is not the enforcement.
- **Tools and the system prompt are identical for every agent of a run**, whatever
  its role, so they share one cache. Role-specific text goes in the first message;
  role limits are enforced when a tool is handled.
- **Every number an agent is told comes from `RunConfig`** and is the number the
  code enforces.
- **Agents are told every limit that affects them**, in plain words.
- **An agent's conversation is append-only.** The only thing that shortens it is a
  compaction.
- **State is written before the next step**, so a killed run resumes. Anything new
  that a run needs to continue goes in the database or the run folder, not in memory.
- **The orchestrator never trusts a path an agent gave it.** Files in agent folders
  are read through `safeio`; the sandbox never mounts the user's home.
- **A change to the sandbox comes with an escape test.**
- **Run data never goes in the repository.** Machine-specific settings go in
  `*.local.*` files.
- **Whoever holds a task decides how to do it.** Prompts and briefs hand out a result
  to reach and how it is checked, never the steps. An agent goes back to whoever gave
  it the task only for something it can't do without, or to say a premise is false.
- **One task has one owner, and only the owner changes what the task is.** A worker
  may contest a premise, never override it. The owner's answer is stored on the task
  (`tasks.decisions`) and shown to the reviewer; a new way to change a running task
  goes through the owner and is recorded the same way.
- **No one hands work to a stronger model than its own** (worker <= lead <= planner,
  `RunConfig.problems`). The user picks the models before the run; agents don't.
- **Splitting is bounded by checks, not by trust**: depth (`max_depth`), width
  (`max_subtasks`, never one piece), money (a sub-team spends its parent task's
  budget) and scope (only the lead sets the goal and the store's structure). A new
  way to hand work down gets the same four bounds.
- Tests drive whole runs with a scripted model (`tests/conftest.py`); a behavior
  change gets a test there. `uv run pytest` must pass before a commit.
- **Every run against the real API gets an entry in `smoke-log.local.md`**
  (gitignored): the command, the setup, the cost per agent, and what it showed,
  including what went wrong. Its run folder is kept. Read the log before changing how
  agents are prompted or scheduled.
