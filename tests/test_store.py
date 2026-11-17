"""The store's gates and signals."""

from __future__ import annotations

import json

from conftest import COLLECTIONS, record
from taskswarm.store import Store, schema_problems


def make(tmp_path, collections=COLLECTIONS) -> Store:
    store = Store(tmp_path / "store")
    assert store.merge_problems(collections) == []
    store.merge(collections)
    return store


def lines(store: Store, name: str) -> list[dict]:
    return [json.loads(l) for l in (store.root / f"{name}.jsonl").read_text().splitlines()]


def test_good_record_is_stored_and_stamped(tmp_path):
    store = make(tmp_path)
    res = store.write("companies", [record("Acme", employees=12, kind="maker", quote="Acme makes it")], "t1", "t1")
    assert (res.accepted, res.rejected) == (1, 0)
    [rec] = lines(store, "companies")
    assert rec["name"] == "Acme" and rec["_by"] == "t1" and rec["_task"] == "t1" and rec["_at"].endswith("Z")


def test_gates_refuse_and_say_why(tmp_path):
    store = make(tmp_path)
    bad = {
        "no source": {"name": "A", "retrieved": "2026-11-01"},
        "no date": {"name": "A", "source": "https://example.com"},
        "source not a url": record("A", source="I remember it"),
        "future": record("A", retrieved="2999-01-01"),
        "not a date": record("A", retrieved="yesterday"),
        "unknown field": record("A", revenue=5),
        "wrong type": record("A", employees="many"),
        "bool is not an integer": record("A", employees=True),
        "below min": record("A", employees=-1),
        "not in enum": record("A", kind="friend"),
        "missing required": {"source": "https://example.com", "retrieved": "2026-11-01"},
        "not an object": "Acme",
        "long quote": record("A", quote="x" * 501),
    }
    for why, rec in bad.items():
        res = store.write("companies", [rec], "t1", "t1")
        assert (res.accepted, res.rejected) == (0, 1), why
        assert res.reasons, why
    assert lines(store, "companies") == []
    text = store.write("companies", [bad["no source"], bad["unknown field"]], "t1", "t1").text("companies")
    assert "`source` is required" in text and "unknown field `revenue`" in text


def test_agents_cannot_forge_stamps(tmp_path):
    store = make(tmp_path)
    store.write("companies", [record("Acme") | {"_by": "lead", "_task": "other"}], "t1", "t1")
    assert lines(store, "companies")[0]["_by"] == "t1"


def test_duplicates_and_conflicts(tmp_path):
    store = make(tmp_path)
    store.write("companies", [record("Acme", employees=10)], "t1", "t1")
    # same key, same values, another source: already there
    res = store.write("companies", [record("  acme ", employees=10, source="https://other.example/a")], "t2", "t2")
    assert (res.accepted, res.duplicates, res.conflicts) == (0, 1, 0)
    # same key, different values: a conflict, logged, the first one kept
    res = store.write("companies", [record("Acme", employees=99)], "t2", "t2")
    assert (res.accepted, res.conflicts) == (0, 1)
    assert [r["employees"] for r in lines(store, "companies")] == [10]
    [sig] = lines(store, "signals")
    assert sig["kind"] == "conflict" and sig["not_stored"]["employees"] == 99 and store.signals == 1


def test_state_survives_reopening(tmp_path):
    store = make(tmp_path)
    store.write("companies", [record("Acme", employees=10)], "t1", "t1")
    store.write("companies", [record("Acme", employees=11)], "t1", "t1")
    again = Store(tmp_path / "store")
    assert again.counts["companies"] == 1 and again.signals == 1
    assert again.write("companies", [record("Acme", employees=10)], "t1", "t1").duplicates == 1


def test_derived_collection_needs_no_source(tmp_path):
    store = make(tmp_path, {"totals": {"derived": True, "fields": {"n": {"type": "integer", "required": True}}}})
    assert store.write("totals", [{"n": 3}], "lead", None).accepted == 1


def test_schema_gates():
    assert schema_problems({"companies": {"fields": {"name": {"type": "string"}}}}) == []
    problems = {
        "bad name": {"Companies": {"fields": {"name": {"type": "string"}}}},
        "no fields": {"companies": {}},
        "bad type": {"companies": {"fields": {"name": {"type": "text"}}}},
        "redeclares source": {"companies": {"fields": {"source": {"type": "url"}}}},
        "key not a field": {"companies": {"fields": {"name": {"type": "string"}}, "key": ["id"]}},
        "key not required": {"companies": {"fields": {"name": {"type": "string"}}, "key": ["name"]}},
        "unknown setting": {"companies": {"fields": {"name": {"type": "string", "unique": True}}}},
        "reserved": {"signals": {"fields": {"name": {"type": "string"}}}},
    }
    for why, schema in problems.items():
        assert schema_problems(schema), why


def test_schema_cannot_break_stored_records(tmp_path):
    store = make(tmp_path)
    store.write("companies", [record("Acme")], "t1", "t1")
    fields = COLLECTIONS["companies"]["fields"]
    grown = {"companies": COLLECTIONS["companies"] | {"fields": fields | {"city": {"type": "string"}}}}
    assert store.merge_problems(grown) == []
    for change in (
        {"fields": {k: v for k, v in fields.items() if k != "employees"}},                 # removes a field
        {"fields": fields | {"employees": {"type": "string"}}},                            # changes a type
        {"fields": fields | {"city": {"type": "string", "required": True}}},               # new required field
        {"key": []},                                                                       # changes the key
    ):
        assert store.merge_problems({"companies": COLLECTIONS["companies"] | change}), change
