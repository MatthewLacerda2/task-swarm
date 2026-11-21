"""The tools agents call, and which role may call which.

Every agent of a run gets the same tool list, whatever its role, so the start of
every request is identical and is read from one shared cache. A tool called by the
wrong role is refused when it is handled (handlers.py).
"""

from __future__ import annotations

_TASK = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Short name, e.g. `prices-eu`: a-z, 0-9 and -, starting with a letter."},
        "title": {"type": "string"},
        "brief": {
            "type": "string",
            "description": "All the worker will know. What to produce, what is already known or ruled out, which "
                           "store collections or files the output goes to, and what is out of scope.",
        },
        "verify": {
            "type": "string",
            "description": "How a reviewer who hasn't seen the work checks it: what to open, sample, re-run or count.",
        },
        "blocked_by": {
            "type": "array", "items": {"type": "string"},
            "description": "Tasks that must pass review first. Their reports are handed to this task's worker.",
        },
        "checkpoint": {
            "type": "string",
            "description": "Only for a task that tests something the tasks blocked by it rest on and that is not "
                           "yet known to work: an estimate, a method, a source. Write it as a question with a yes or "
                           "no answer (\"Can a shop's revenue be estimated from its listing better than a plain "
                           "guess?\"). The reviewer answers it, and the tasks blocked by this one wait until you "
                           "have read the result and ruled with `decide`: go on, or change the plan first.",
        },
    },
    "required": ["id", "title", "brief", "verify"],
}

TOOLS = [
    {"type": "bash_20250124", "name": "bash"},
    {
        "name": "plan",
        "description": (
            "Set or change the plan. The first call needs `goal` and `done_when`. Later calls only need what "
            "changes: new tasks, redefined tasks that haven't started (same id), tasks to cancel, new collections "
            "or new fields. The whole change is checked first; if anything is wrong nothing changes and you are "
            "told every problem. Tasks with nothing blocking them start right away. A worker can call it with "
            "only `tasks` and `cancel`, to split its own task into sub-tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What the finished work is, in a sentence or two."},
                "done_when": {
                    "type": "string",
                    "description": "What a reviewer holding only the deliverable and the store checks to decide the "
                                   "goal is met.",
                },
                "collections": {
                    "type": "object",
                    "description": (
                        "The store's structure: collection name -> {description, fields, key, derived}. `fields` "
                        "maps a field name to {type, required, enum, min, max, description}; type is one of string, "
                        "integer, number, boolean, date, url. `key` lists the fields that identify a record (they "
                        "must be required). Every record also carries `source` (URL), `retrieved` (date) and "
                        "optionally `quote`; set `derived: true` for a collection computed from other records, "
                        "which doesn't need them."
                    ),
                },
                "tasks": {"type": "array", "items": _TASK},
                "cancel": {"type": "array", "items": {"type": "string"}, "description": "Ids of tasks to cancel."},
            },
        },
    },
    {
        "name": "send_message",
        "description": (
            "Send a short message to another agent by name: `lead`, or a task's worker by its task id. It arrives "
            "with the result of the recipient's next command; an agent that has finished is woken with its memory "
            "intact to answer. Use it for a question or a decision that isn't yours to make, not for results: "
            "results go in your report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "message": {"type": "string"}},
            "required": ["to", "message"],
        },
    },
    {
        "name": "post",
        "description": (
            "Post a short note to a channel, for the other agents: something you learned that saves them time or "
            "a mistake (a site that blocks or limits requests, a source that works, a trap in the data). A channel "
            "is named by its theme; posting to a name that doesn't exist creates it. The others see it with the "
            "result of their next command. A post wakes nobody and changes no task: a question or a decision goes "
            "by `send_message`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "The theme, e.g. `github-api`: a-z, 0-9 and -."},
                "text": {"type": "string", "description": "Brief: one or two sentences is usually enough."},
            },
            "required": ["channel", "text"],
        },
    },
    {
        "name": "store_write",
        "description": (
            "Add records to a store collection. Give `records` (a list of objects) for a few, or `path` to a JSON "
            "Lines file in your folder (one record per line) for many. Each record is checked against the "
            "collection's fields and must carry `source` and `retrieved` unless the collection is derived. You are "
            "told how many were accepted and why the others were refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "records": {"type": "array", "items": {"type": "object"}},
                "path": {"type": "string"},
            },
            "required": ["collection"],
        },
    },
    {
        "name": "store_update",
        "description": (
            "Correct stored records: set new values for some fields of the records picked by `ids` or by `where`. "
            "The corrected records must pass the same checks as new ones. If any of them doesn't, or isn't yours "
            "to change, nothing changes and you are told why."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"},
                        "description": "The `_id` of each record, as stored."},
                "where": {"type": "object",
                          "description": "Field values a record must have, all of them, e.g. {\"shop\": \"Acme\"}. "
                                         "Stamps work too: {\"_task\": \"prices\"}."},
                "reason": {"type": "string", "description": "Why, in a sentence. It is logged."},
                "set": {"type": "object", "description": "Field -> new value. null removes an optional field."},
            },
            "required": ["collection", "set", "reason"],
        },
    },
    {
        "name": "store_delete",
        "description": (
            "Remove stored records, picked by `ids` or by `where`: records that are wrong, or that should never "
            "have been stored. If any of them isn't yours to change, nothing is removed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"},
                        "description": "The `_id` of each record, as stored."},
                "where": {"type": "object",
                          "description": "Field values a record must have, all of them, e.g. {\"shop\": \"Acme\"}. "
                                         "Stamps work too: {\"_task\": \"prices\"}."},
                "reason": {"type": "string", "description": "Why, in a sentence. It is logged."},
            },
            "required": ["collection", "reason"],
        },
    },
    {
        "name": "report",
        "description": (
            "Worker only. Hand in your task. This ends your work on it; a reviewer then checks it against the "
            "task's `verify`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "The result first, then what the lead needs to know to use it: where the output "
                                   "is and what is uncertain.",
                },
                "not_done": {
                    "type": "string",
                    "description": "Each thing the brief or `verify` asks for that you are not delivering, and why "
                                   "it could not be done: what you tried and what stopped you. Leave it out when "
                                   "everything asked for is delivered.",
                },
                "how": {"type": "string", "description": "Optional, brief: how you went about it."},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "decide",
        "description": (
            "Owner of a task only (the lead for its tasks, a worker for its sub-tasks). Settle a task that waits "
            "for you. One whose review ended in `partial`: what it delivered is right, something asked for is "
            "missing, and its worker can't supply it by trying again. Or a checkpoint that passed review: its "
            "reviewer answered whether what the plan builds on it holds, and the tasks blocked by it wait for your "
            "ruling. Accept it, or send it back with what to do instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "accept": {
                    "type": "boolean",
                    "description": "true: the task is done as delivered; what is missing stays recorded on it and "
                                   "is shown to the tasks that build on it and to the final reviewer. false: the "
                                   "worker continues, under what you say in `note`.",
                },
                "note": {
                    "type": "string",
                    "description": "With accept false, required: what the worker should do instead (a smaller "
                                   "target, another source, a different method). It is recorded on the task as your "
                                   "decision and the next reviewer checks the work against it. With accept true, "
                                   "optional: why what exists is enough. For a checkpoint it is required: your "
                                   "ruling, which is what the tasks blocked by it are told. Say why the plan stands, "
                                   "or how you changed it (change it with `plan` first).",
                },
            },
            "required": ["task", "accept"],
        },
    },
    {
        "name": "verdict",
        "description": "Reviewer only. Give your verdict. This ends your review.",
        "input_schema": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "enum": ["pass", "partial", "fail"],
                    "description": "pass: it meets what was asked. fail: something is wrong, or missing and within "
                                   "the worker's reach; it goes back to be fixed. partial: what was delivered is "
                                   "right, but something asked for is missing and trying again would not get it; "
                                   "whoever owns the work decides what happens next.",
                },
                "findings": {
                    "type": "string",
                    "description": "What you checked and how. With fail: each problem, specific enough to fix "
                                   "without asking you.",
                },
                "missing": {
                    "type": "string",
                    "description": "With partial only: each thing asked for that is not delivered, and what you did "
                                   "to check that it can't be.",
                },
                "holds": {
                    "type": "string",
                    "enum": ["yes", "no", "unclear"],
                    "description": "Only when the task is a checkpoint, with pass or partial: your answer to the "
                                   "checkpoint's question, from the result. Work that is correct and shows the "
                                   "answer is no still passes, with `no` here.",
                },
            },
            "required": ["result", "findings"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Lead only. Hand in the deliverable: a file in your folder. It is then reviewed against `done_when`. "
            "Refused while tasks are still open."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The deliverable file, in your folder."},
                "summary": {"type": "string", "description": "A few lines for the user: what was found and its limits."},
            },
            "required": ["path", "summary"],
        },
    },
]

ALLOWED = {
    "planner": {"bash", "plan"},
    "lead": {"bash", "plan", "decide", "send_message", "post", "store_write", "store_update", "store_delete",
             "finish"},
    "worker": {"bash", "plan", "decide", "send_message", "post", "store_write", "store_update", "store_delete",
               "report"},
    "reviewer": {"bash", "send_message", "post", "verdict"},
}
