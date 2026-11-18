"""The store: what the team has found, in a structure decided before the work starts.

The lead declares collections and their fields in the plan. Every write goes through
the same gates, so nothing lands in the store "make it up as we go":

  - the collection exists and the record has no field the schema doesn't declare;
  - required fields are present and every value has its declared type and range;
  - every record says where it came from: `source` (an http(s) URL) and `retrieved`
    (the date it was read). A collection declared `derived` (computed from other
    records, not read from a page) is exempt.

A gate refuses the record and says why. A signal lets it through and is kept for
the lead and the reviewers: two records with the same key and different values are
a conflict, the first is kept, and both are written to signals.jsonl.

A stored record can be corrected or removed (`change`), by whoever the caller says
may: the result has to pass the same gates, the whole change is applied or none of
it, and what was done to which records, by whom and why goes in changes.jsonl. The
old content is not kept there, so a record removed for holding something it should
not hold is really gone.

Records are plain JSON lines under <run>/store/<collection>.jsonl, read-only inside
the sandboxes at /task/store, so agents read them with any tool. The orchestrator
stamps each record with its number (`_id`), who wrote it (`_by`, `_task`) and when
(`_at`), and who last changed it (`_edited_by`, `_edited_at`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

TYPES = ("string", "integer", "number", "boolean", "date", "url")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Fields every record may carry; the first two are required unless the collection is derived.
UNIVERSAL = {
    "source": {"type": "url"},
    "retrieved": {"type": "date"},
    "quote": {"type": "string", "max_length": 500},   # the words on the page that support the record
}
STAMPS = ("_id", "_by", "_task", "_at", "_edited_by", "_edited_at")
MAX_REASONS = 10


def schema_problems(collections: object) -> list[str]:
    """Everything wrong with a schema, in words the lead can act on."""
    if not isinstance(collections, dict):
        return ["`collections` must be an object mapping a collection name to its definition"]
    out: list[str] = []
    for name, c in collections.items():
        where = f"collection `{name}`"
        if not isinstance(name, str) or not NAME_RE.match(name) or name in ("signals", "schema"):
            out.append(f"{where}: the name must be lowercase letters, digits and _ (start with a letter), "
                       "and not `signals` or `schema`")
        if not isinstance(c, dict):
            out.append(f"{where}: must be an object with `fields`")
            continue
        unknown = set(c) - {"description", "fields", "key", "derived"}
        if unknown:
            out.append(f"{where}: unknown settings {sorted(unknown)}; allowed: description, fields, key, derived")
        fields = c.get("fields")
        if not isinstance(fields, dict) or not fields:
            out.append(f"{where}: `fields` must list at least one field")
            continue
        for fname, spec in fields.items():
            fwhere = f"{where}, field `{fname}`"
            if not isinstance(fname, str) or not NAME_RE.match(fname):
                out.append(f"{fwhere}: the name must be lowercase letters, digits and _ (start with a letter)")
            elif fname in UNIVERSAL:
                out.append(f"{fwhere}: every record already has `source`, `retrieved` and `quote`; don't declare them")
            if not isinstance(spec, dict) or spec.get("type") not in TYPES:
                out.append(f"{fwhere}: needs a `type`, one of {', '.join(TYPES)}")
                continue
            unknown = set(spec) - {"type", "required", "enum", "min", "max", "description"}
            if unknown:
                out.append(f"{fwhere}: unknown settings {sorted(unknown)}; allowed: type, required, enum, min, max, "
                           "description")
            if "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
                out.append(f"{fwhere}: `enum` must be a non-empty list")
        key = c.get("key", [])
        if not isinstance(key, list) or any(k not in fields for k in key):
            out.append(f"{where}: `key` must be a list of this collection's own fields")
        elif any(not fields[k].get("required") for k in key if isinstance(fields.get(k), dict)):
            out.append(f"{where}: every `key` field must be `required`")
    return out


def _value_problem(value: object, spec: dict) -> str | None:
    t = spec["type"]
    if t == "string":
        if not isinstance(value, str) or not value.strip():
            return "must be a non-empty string"
        if "max_length" in spec and len(value) > spec["max_length"]:
            return f"must be at most {spec['max_length']} characters"
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "must be an integer"
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return "must be a number"
    elif t == "boolean":
        if not isinstance(value, bool):
            return "must be true or false"
    elif t == "date":
        if not isinstance(value, str) or not DATE_RE.match(value):
            return "must be a date written YYYY-MM-DD"
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            return "is not a real date"
    elif t == "url":
        if not isinstance(value, str):
            return "must be an http(s) URL"
        u = urlparse(value)
        if u.scheme not in ("http", "https") or not u.netloc or " " in value:
            return "must be an http(s) URL"
    if "enum" in spec and value not in spec["enum"]:
        return f"must be one of {spec['enum']}"
    if t in ("integer", "number"):
        if "min" in spec and value < spec["min"]:
            return f"must be at least {spec['min']}"
        if "max" in spec and value > spec["max"]:
            return f"must be at most {spec['max']}"
    return None


def record_problem(record: object, collection: dict, today: dt.date) -> str | None:
    """The first gate this record fails, or None."""
    if not isinstance(record, dict):
        return "a record must be a JSON object"
    fields = collection["fields"]
    derived = bool(collection.get("derived"))
    for name in record:
        if name not in fields and name not in UNIVERSAL:
            return f"unknown field `{name}`: the schema doesn't declare it"
    for name, spec in fields.items():
        value = record.get(name)
        if value is None:
            if spec.get("required"):
                return f"`{name}` is required"
            continue
        if problem := _value_problem(value, spec):
            return f"`{name}` {problem}"
    for name, spec in UNIVERSAL.items():
        value = record.get(name)
        if value is None:
            if not derived and name != "quote":
                return f"`{name}` is required: every record says where it came from"
            continue
        if problem := _value_problem(value, spec):
            return f"`{name}` {problem}"
    if record.get("retrieved") and dt.date.fromisoformat(record["retrieved"]) > today + dt.timedelta(days=1):
        return "`retrieved` is in the future"
    return None


@dataclass
class WriteResult:
    accepted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    rejected: int = 0
    reasons: Counter = field(default_factory=Counter)
    examples: list[str] = field(default_factory=list)

    def text(self, collection: str) -> str:
        lines = [f"{collection}: {self.accepted} accepted, {self.rejected} rejected, "
                 f"{self.duplicates} already there, {self.conflicts} in conflict with an existing record."]
        if self.conflicts:
            lines.append("A conflict is a record with the same key as a stored one and different values. The stored "
                         "one was kept and both were logged in /task/store/signals.jsonl. If the stored one is the "
                         "wrong one, correct it with `store_update`; otherwise say so in your report.")
        if self.rejected:
            lines.append("Rejected (nothing was written for these):")
            lines += [f"- {n} x {reason}" for reason, n in self.reasons.most_common(MAX_REASONS)]
            lines += [f"  e.g. {e}" for e in self.examples[:3]]
        return "\n".join(lines)


@dataclass
class ChangeResult:
    changed: int = 0
    problems: list[str] = field(default_factory=list)      # why nothing was changed

    def text(self, collection: str, action: str) -> str:
        if self.problems:
            return "Nothing was changed:\n" + "\n".join(f"- {p}" for p in self.problems[:MAX_REASONS + 2])
        done = "deleted" if action == "delete" else "updated"
        return f"{collection}: {self.changed} records {done}. Logged in /task/store/changes.jsonl."


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.schema: dict = {}
        path = root / "schema.json"
        if path.exists():
            self.schema = json.loads(path.read_text())
        # collection -> key tuple -> digest of the declared fields
        self.keys: dict[str, dict[tuple, str]] = {}
        self.counts: Counter = Counter()
        self.last_id: Counter = Counter()        # collection -> the highest `_id` given so far
        self.signals = self.changes = 0
        for name in self.schema:
            self._load(name)
        for attr in ("signals", "changes"):
            log = root / f"{attr}.jsonl"
            if log.exists():
                with log.open() as f:
                    setattr(self, attr, sum(1 for _ in f))
        log = root / "changes.jsonl"
        if log.exists():           # a deleted record's number is never given again
            for line in log.read_text().splitlines():
                try:
                    entry = json.loads(line)
                    self.last_id[entry["collection"]] = max(self.last_id[entry["collection"]], *entry["ids"])
                except (ValueError, KeyError, TypeError):
                    continue

    def _path(self, collection: str) -> Path:
        return self.root / f"{collection}.jsonl"

    def _records(self, name: str):
        path = self._path(name)
        if not path.exists():
            return
        with path.open() as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue   # a line cut short by a crash

    def _rewrite(self, name: str, records: Iterable[dict]) -> None:
        """Replace a collection's file in one step, so a reader sees the old file or the new one."""
        tmp = self.root / f".{name}.jsonl.tmp"
        with tmp.open("w") as out:
            for rec in records:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, self._path(name))

    def _load(self, name: str) -> None:
        self.keys[name] = {}
        self.counts[name] = 0
        unnumbered = False
        for rec in self._records(name):
            self.counts[name] += 1
            if "_id" in rec:
                self.last_id[name] = max(self.last_id[name], rec["_id"])
            else:
                unnumbered = True
            if key := self._key(name, rec):
                self.keys[name][key] = self._digest(name, rec)
        if unnumbered:         # a store written before records were numbered
            def numbered():
                for rec in self._records(name):
                    if "_id" not in rec:
                        self.last_id[name] += 1
                        rec = {**rec, "_id": self.last_id[name]}
                    yield rec
            self._rewrite(name, list(numbered()))

    def _key(self, name: str, rec: dict) -> tuple | None:
        key = self.schema[name].get("key") or []
        return tuple(_norm(rec.get(k)) for k in key) if key else None

    def _digest(self, name: str, rec: dict) -> str:
        body = {k: _norm(rec.get(k)) for k in sorted(self.schema[name]["fields"]) if rec.get(k) is not None}
        return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    # --- schema ----------------------------------------------------------------------

    def merge_problems(self, collections: dict) -> list[str]:
        """What stops `collections` from being merged into the current schema."""
        out = schema_problems(collections)
        if out:
            return out
        for name, new in collections.items():
            old = self.schema.get(name)
            if not old or not self.counts[name]:
                continue
            where = f"collection `{name}` already holds {self.counts[name]} records, so"
            if (old.get("key") or []) != (new.get("key") or []) or bool(old.get("derived")) != bool(new.get("derived")):
                out.append(f"{where} its `key` and `derived` can't change; make a new collection instead")
            for fname, spec in old["fields"].items():
                if fname not in new["fields"]:
                    out.append(f"{where} field `{fname}` can't be removed")
                elif new["fields"][fname].get("type") != spec["type"]:
                    out.append(f"{where} field `{fname}` can't change type")
            for fname, spec in new["fields"].items():
                if fname not in old["fields"] and spec.get("required"):
                    out.append(f"{where} the new field `{fname}` can't be required")
        return out

    def merge(self, collections: dict) -> None:
        with self.lock:
            for name, c in collections.items():
                self.schema[name] = c
                self.keys.setdefault(name, {})
                self._path(name).touch()
            tmp = self.root / "schema.json.tmp"
            tmp.write_text(json.dumps(self.schema, indent=1, ensure_ascii=False))
            os.replace(tmp, self.root / "schema.json")

    # --- writes ----------------------------------------------------------------------

    def write(self, collection: str, records: Iterable[object], agent: str, task: str | None) -> WriteResult:
        """Append the records that pass the gates. Raises KeyError for an unknown collection."""
        spec = self.schema[collection]
        res = WriteResult()
        today = dt.datetime.now(dt.UTC).date()
        now = _now()
        with self.lock, self._path(collection).open("a") as out, (self.root / "signals.jsonl").open("a") as sig:
            for rec in records:
                if isinstance(rec, dict):
                    rec = {k: v for k, v in rec.items() if k not in STAMPS}
                if problem := record_problem(rec, spec, today):
                    res.rejected += 1
                    res.reasons[problem] += 1
                    if len(res.examples) < 3:
                        res.examples.append(json.dumps(rec, ensure_ascii=False)[:300])
                    continue
                key = self._key(collection, rec)
                digest = self._digest(collection, rec)
                stamped = rec | {"_id": self.last_id[collection] + 1, "_by": agent, "_task": task, "_at": now}
                if key is not None and key in self.keys[collection]:
                    if self.keys[collection][key] == digest:
                        res.duplicates += 1
                    else:
                        res.conflicts += 1
                        self.signals += 1
                        sig.write(json.dumps({"kind": "conflict", "collection": collection, "key": list(key),
                                              "not_stored": stamped}, ensure_ascii=False) + "\n")
                    continue
                out.write(json.dumps(stamped, ensure_ascii=False) + "\n")
                self.last_id[collection] += 1
                if key is not None:
                    self.keys[collection][key] = digest
                self.counts[collection] += 1
                res.accepted += 1
        return res

    # --- corrections -----------------------------------------------------------------

    def change(self, collection: str, agent: str, may_change: Callable[[dict], bool], reason: str,
               ids: list | None = None, where: dict | None = None, new_values: dict | None = None) -> ChangeResult:
        """Update (`new_values` given) or delete the records picked by `ids` or by `where`.

        `where` maps a field to the value it must have; all must match. `may_change` says
        whether the caller may touch a record. Everything is checked first: if any picked
        record may not be changed, or any updated record would fail a gate, nothing changes.
        """
        spec = self.schema[collection]
        res = ChangeResult()
        action = "delete" if new_values is None else "update"
        known = set(spec["fields"]) | set(UNIVERSAL)
        if where is not None:
            if unknown := sorted(set(where) - known - set(STAMPS)):
                res.problems.append(f"`where` names fields the collection doesn't have: {', '.join(unknown)}")
        if new_values is not None:
            if unknown := sorted(set(new_values) - known):
                res.problems.append(f"`set` names fields the collection doesn't declare: {', '.join(unknown)}")
        if res.problems:
            return res
        wanted = set(ids) if ids is not None else None
        target = {k: _norm(v) for k, v in (where or {}).items()}

        def picked(rec: dict) -> bool:
            if wanted is not None:
                return rec.get("_id") in wanted
            return all(_norm(rec.get(k)) == v for k, v in target.items())

        today = dt.datetime.now(dt.UTC).date()
        now = _now()
        with self.lock:
            hits = [rec for rec in self._records(collection) if picked(rec)]
            if not hits:
                res.problems.append("no stored record matches")
                return res
            if wanted is not None and (missing := sorted(wanted - {r["_id"] for r in hits})):
                res.problems.append(f"there is no record with `_id` {', '.join(map(str, missing[:10]))}")
            others = Counter(str(r.get("_task") or r.get("_by")) for r in hits if not may_change(r))
            if others:
                res.problems.append(
                    f"{sum(others.values())} of the {len(hits)} matching records were written under other tasks "
                    f"({', '.join(sorted(others))}). You can change records you wrote and records written by tasks "
                    "you own; `lead` can change any. Ask with `send_message`.")
            new: dict[int, dict] = {}
            if action == "update" and not res.problems:
                # keys held by the records that stay as they are
                taken = dict(self.keys[collection])
                for r in hits:
                    taken.pop(self._key(collection, r), None)
                seen: dict[tuple, int] = {}
                for r in hits:
                    body = {k: v for k, v in r.items() if k not in STAMPS} | new_values
                    body = {k: v for k, v in body.items() if v is not None}
                    if problem := record_problem(body, spec, today):
                        res.problems.append(f"record {r['_id']}: {problem}")
                        continue
                    key = self._key(collection, body)
                    if key is not None and (key in taken or key in seen):
                        res.problems.append(f"record {r['_id']}: another record already has the key "
                                            f"{', '.join(map(str, key))}")
                        continue
                    if key is not None:
                        seen[key] = r["_id"]
                    stamps = {k: r[k] for k in STAMPS if k in r}
                    new[r["_id"]] = body | stamps | {"_edited_by": agent, "_edited_at": now}
            if res.problems:
                return res
            gone = {r["_id"] for r in hits}
            self._rewrite(collection, (new[r["_id"]] if r.get("_id") in new else r
                                       for r in self._records(collection)
                                       if action == "update" or r.get("_id") not in gone))
            with (self.root / "changes.jsonl").open("a") as log:
                entry = {"at": now, "by": agent, "collection": collection, "action": action,
                         "ids": sorted(gone), "reason": reason}
                if action == "update":
                    entry["fields"] = sorted(new_values)
                log.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.changes += 1
            self._load(collection)
            res.changed = len(hits)
        return res

    # --- what agents are shown -------------------------------------------------------

    def describe(self) -> str:
        if not self.schema:
            return "The store has no collections."
        lines = []
        for name, c in self.schema.items():
            head = f"- `{name}` ({self.counts[name]} records)"
            if c.get("derived"):
                head += ", derived"
            if c.get("key"):
                head += f", key: {', '.join(c['key'])}"
            if c.get("description"):
                head += f": {c['description']}"
            lines.append(head)
            for fname, spec in c["fields"].items():
                bits = [spec["type"]]
                if spec.get("required"):
                    bits.append("required")
                if "enum" in spec:
                    bits.append("one of " + ", ".join(map(str, spec["enum"])))
                if "min" in spec:
                    bits.append(f"min {spec['min']}")
                if "max" in spec:
                    bits.append(f"max {spec['max']}")
                desc = f": {spec['description']}" if spec.get("description") else ""
                lines.append(f"  - `{fname}` ({', '.join(bits)}){desc}")
        return "\n".join(lines)


def _norm(value: object) -> object:
    return " ".join(value.split()).casefold() if isinstance(value, str) else value
