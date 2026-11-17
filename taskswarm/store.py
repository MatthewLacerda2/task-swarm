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

Records are plain JSON lines under <run>/store/<collection>.jsonl, read-only inside
the sandboxes at /task/store, so agents read them with any tool. The orchestrator
stamps who wrote each record (`_by`, `_task`) and when (`_at`).
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
from typing import Iterable
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
STAMPS = ("_by", "_task", "_at")
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
                         "one was kept and both were logged in /task/store/signals.jsonl. Say so in your report.")
        if self.rejected:
            lines.append("Rejected (nothing was written for these):")
            lines += [f"- {n} x {reason}" for reason, n in self.reasons.most_common(MAX_REASONS)]
            lines += [f"  e.g. {e}" for e in self.examples[:3]]
        return "\n".join(lines)


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
        self.signals = 0
        for name in self.schema:
            self._load(name)
        sig = root / "signals.jsonl"
        if sig.exists():
            with sig.open() as f:
                self.signals = sum(1 for _ in f)

    def _path(self, collection: str) -> Path:
        return self.root / f"{collection}.jsonl"

    def _load(self, name: str) -> None:
        self.keys[name] = {}
        path = self._path(name)
        if not path.exists():
            return
        with path.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue   # a line cut short by a crash
                self.counts[name] += 1
                if key := self._key(name, rec):
                    self.keys[name][key] = self._digest(name, rec)

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
        now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
                stamped = rec | {"_by": agent, "_task": task, "_at": now}
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
                if key is not None:
                    self.keys[collection][key] = digest
                self.counts[collection] += 1
                res.accepted += 1
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
