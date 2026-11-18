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


# --- correcting and removing records ------------------------------------------------------

ANYONE = lambda rec: True


def changes(store: Store) -> list[dict]:
    return [json.loads(l) for l in (store.root / "changes.jsonl").read_text().splitlines()]


def test_records_are_numbered_and_can_be_updated(tmp_path):
    store = make(tmp_path)
    store.write("companies", [record("Acme", employees=12), record("Bolt"), record("Coil", kind="maker")], "t1", "t1")
    assert [r["_id"] for r in lines(store, "companies")] == [1, 2, 3]

    res = store.change("companies", "t1", ANYONE, "the page says 15", ids=[1], new_values={"employees": 15})
    assert res.changed == 1 and res.text("companies", "update").startswith("companies: 1 records updated")
    one = lines(store, "companies")[0]
    assert (one["employees"], one["_id"], one["_by"], one["_edited_by"]) == (15, 1, "t1", "t1")
    assert one["_edited_at"].endswith("Z") and one["source"] == "https://example.com/Acme"
    # null removes an optional field; `where` picks by value, forgiving about case like the key is
    assert store.change("companies", "t1", ANYONE, "kind was a guess", where={"name": "coil"},
                        new_values={"kind": None}).changed == 1
    assert "kind" not in lines(store, "companies")[2]
    # the log says what was done to which records and why, and does not keep the old content
    assert changes(store) == [
        {"at": one["_edited_at"], "by": "t1", "collection": "companies", "action": "update", "ids": [1],
         "reason": "the page says 15", "fields": ["employees"]},
        {"at": changes(store)[1]["at"], "by": "t1", "collection": "companies", "action": "update", "ids": [3],
         "reason": "kind was a guess", "fields": ["kind"]},
    ]
    # a corrected record is the stored one from now on: writing it again is a duplicate, not a conflict
    again = store.write("companies", [record("Acme", employees=15)], "t2", "t2")
    assert (again.duplicates, again.conflicts) == (1, 0)


def test_an_update_is_checked_like_a_new_record_and_is_all_or_nothing(tmp_path):
    store = make(tmp_path)
    store.write("companies", [record("Acme", employees=12), record("Bolt", employees=3)], "t1", "t1")
    before = lines(store, "companies")

    def refused(needle, **kw):
        res = store.change("companies", "t1", ANYONE, "testing the gates", **kw)
        assert res.changed == 0 and needle in res.text("companies", "update"), res.problems
        assert lines(store, "companies") == before and not (store.root / "changes.jsonl").exists()

    refused("record 2: `employees` must be at least 0", where={"_task": "t1"}, new_values={"employees": -1} | {})
    refused("`employees` must be an integer", ids=[1], new_values={"employees": "many"})
    refused("`set` names fields the collection doesn't declare: revenue", ids=[1], new_values={"revenue": 5})
    refused("record 1: `name` is required", ids=[1], new_values={"name": None})
    refused("record 1: `source` is required", ids=[1], new_values={"source": None})
    refused("record 1: another record already has the key bolt", ids=[1], new_values={"name": "Bolt"})
    refused("another record already has the key same", where={"_task": "t1"}, new_values={"name": "Same"})
    refused("no stored record matches", where={"name": "Nobody"}, new_values={"employees": 1})
    refused("there is no record with `_id` 9", ids=[1, 9], new_values={"employees": 1})
    refused("`where` names fields the collection doesn't have: colour", where={"colour": "red"},
            new_values={"employees": 1})


def test_delete_removes_for_good_and_numbers_are_not_reused(tmp_path):
    store = make(tmp_path)
    store.write("companies", [record("Acme"), record("Bolt"), record("Coil")], "t1", "t1")
    res = store.change("companies", "lead", ANYONE, "these two hold a person's name", ids=[2, 3])
    assert res.changed == 2 and "2 records deleted" in res.text("companies", "delete")
    assert [r["name"] for r in lines(store, "companies")] == ["Acme"] and store.counts["companies"] == 1
    log = (store.root / "changes.jsonl").read_text()
    assert "Bolt" not in log and "Coil" not in log and '"ids": [2, 3]' in log and store.changes == 1
    # the key is free again, and a new record never takes a deleted one's number, even after a restart
    again = Store(tmp_path / "store")
    assert again.write("companies", [record("Bolt")], "t1", "t1").accepted == 1
    assert [r["_id"] for r in lines(again, "companies")] == [1, 4] and again.changes == 1


def test_only_the_allowed_records_change(tmp_path):
    store = make(tmp_path)
    store.write("companies", [record("Acme")], "a", "a")
    store.write("companies", [record("Bolt")], "b", "b")
    mine = lambda rec: rec["_by"] == "a"
    res = store.change("companies", "a", mine, "cleaning up", where={"retrieved": "2026-11-01"})
    assert res.changed == 0 and "1 of the 2 matching records were written under other tasks (b)" in res.problems[0]
    assert len(lines(store, "companies")) == 2
    assert store.change("companies", "a", mine, "cleaning up", where={"_task": "a"}).changed == 1


def test_a_store_from_before_numbering_gets_numbers(tmp_path):
    store = make(tmp_path)
    old = [record("Acme") | {"_by": "t", "_task": "t", "_at": "2026-11-01T00:00:00Z"}, record("Bolt") | {"_by": "t"}]
    (store.root / "companies.jsonl").write_text("".join(json.dumps(r) + "\n" for r in old))
    again = Store(tmp_path / "store")
    assert [r["_id"] for r in lines(again, "companies")] == [1, 2]
    assert again.write("companies", [record("Coil")], "t", "t").accepted == 1
    assert lines(again, "companies")[2]["_id"] == 3
