import json
import random

import pytest

from collationkey import quality as Q
from collationkey import quality_ref as R
from collationkey import api

SCHEMA = {"tables": [
    {"name": "order", "columns": ["oid", "status"], "key": ["oid"]},
    {"name": "shipment", "columns": ["sid", "oid"], "key": ["sid"]},
    {"name": "audit_flag", "columns": ["sid", "reason"], "key": ["sid"]},
    {"name": "depends", "columns": ["a", "b"], "key": ["a", "b"]},
    {"name": "blocked", "columns": ["oid"], "key": ["oid"]},
]}

RULES = [
    {"id": "known_order", "head": ["known_order", "?oid"],
     "body": [["order", "?oid", "?s"]]},
    {"id": "missing_order", "head": ["violation", "?sid"],
     "body": [["shipment", "?sid", "?oid"]], "not": [["known_order", "?oid"]]},
    {"id": "cancelled_order", "head": ["violation", "?sid"],
     "body": [["shipment", "?sid", "?oid"], ["order", "?oid", "cancelled"]]},
    {"id": "flagged_shipment", "head": ["violation", "?sid"],
     "body": [["audit_flag", "?sid", "?r"]]},
    {"id": "reach_base", "head": ["reachable", "?a", "?b"],
     "body": [["depends", "?a", "?b"]]},
    {"id": "reach_step", "head": ["reachable", "?a", "?c"],
     "body": [["reachable", "?a", "?b"], ["depends", "?b", "?c"]]},
    {"id": "blocked_chain", "head": ["violation", "?oid"],
     "body": [["reachable", "?oid", "?b"], ["blocked", "?b"]]},
]


def make_registry():
    registry = Q.QualityRegistry(SCHEMA)
    rv = registry.register_rules(RULES)
    return registry, rv


def ref_rows(ledger):
    rows = {}
    for table, store in ledger.snapshot().items():
        columns = ledger.tables[table]["columns"]
        rows[table] = [(list(kid), dict(zip(columns, values)))
                       for kid, values in store.items()]
    return rows


def violation_index(result):
    return {tuple(v["identity"]): v for v in result["violations"]}


def ref_index(schema, ledger):
    report = R.summary(schema, ref_rows(ledger), RULES)
    return {tuple(v["identity"]): v for v in report["violations"]}


def key_id(key):
    """API identity form: composite keys are lists, scalar keys stay scalars."""
    return list(key) if isinstance(key, tuple) else key


def assert_engines_agree(registry, assessor, result):
    """Cross-check the incremental engine against the full-recompute reference."""
    expected = ref_index(SCHEMA, registry.ledger)
    actual = violation_index(result)
    assert set(actual) == set(expected)
    for identity, expected_v in expected.items():
        actual_v = actual[identity]
        assert actual_v["support_count"] == expected_v["support_count"]
        actual_bases = {(b["table"], tuple(b["id"])) for b in actual_v["affected_rows"]}
        expected_bases = {(b[0], tuple(b[1])) for b in expected_v["fact_bases"]}
        assert actual_bases == expected_bases
    # Every proof id resolves and every cited support points at a live proof.
    for violation in result["violations"]:
        for pid in violation["proofs"]:
            proof = result["proofs"][pid]
            assert proof["rule_version"] == assessor.rule_version
            for support in proof["support"]:
                if support["proof"] is not None:
                    assert support["proof"] in result["proofs"]
                if support["kind"] == "negation":
                    assert support["fact_version"] == registry.ledger.version
                    assert support["stratum"] < assessor.strata[proof["head"][0]]


def test_violation_with_two_independent_bases_survives_single_retraction():
    registry, rv = make_registry()
    registry.ledger.apply_batch([
        {"op": "add", "table": "order", "id": "o1",
         "values": {"oid": "o1", "status": "cancelled"}},
        {"op": "add", "table": "shipment", "id": "s1",
         "values": {"sid": "s1", "oid": "o1"}},
        {"op": "add", "table": "audit_flag", "id": "s1",
         "values": {"sid": "s1", "reason": "manual"}},
    ])
    assessor = registry.assessor(rv)
    assessor.advance()
    first = registry.publish(rv, assessor)
    s1 = violation_index(first)[("s1",)]
    assert s1["support_count"] == 2
    assert_engines_agree(registry, assessor, first)

    # Withdraw the order: cancelled basis collapses, two other bases survive.
    registry.ledger.apply_batch([{"op": "delete", "table": "order", "id": "o1"}])
    assessor.advance()
    second = registry.publish(rv, assessor)
    s1 = violation_index(second)[("s1",)]
    assert s1["support_count"] == 2
    surviving = {second["proofs"][pid]["rule_id"] for pid in s1["proofs"]}
    assert surviving == {"missing_order", "flagged_shipment"}
    assert_engines_agree(registry, assessor, second)

    # Withdraw the audit flag as well: the last independent basis keeps the
    # alarm open until the shipment itself disappears.
    registry.ledger.apply_batch([{"op": "delete", "table": "audit_flag", "id": "s1"}])
    assessor.advance()
    third = registry.publish(rv, assessor)
    s1 = violation_index(third)[("s1",)]
    assert s1["support_count"] == 1
    assert third["proofs"][s1["proofs"][0]]["rule_id"] == "missing_order"
    registry.ledger.apply_batch([{"op": "delete", "table": "shipment", "id": "s1"}])
    assessor.advance()
    fourth = registry.publish(rv, assessor)
    assert ("s1",) not in violation_index(fourth)
    assert_engines_agree(registry, assessor, fourth)


def test_negation_stratum_flips_both_ways_across_batches():
    registry, rv = make_registry()
    registry.ledger.apply_batch([
        {"op": "add", "table": "shipment", "id": "s9",
         "values": {"sid": "s9", "oid": "o9"}},
    ])
    assessor = registry.assessor(rv)
    assessor.advance()
    result = assessor.findings()
    assert ("s9",) in violation_index(result)

    registry.ledger.apply_batch([
        {"op": "add", "table": "order", "id": "o9",
         "values": {"oid": "o9", "status": "ok"}},
    ])
    assessor.advance()
    assert ("s9",) not in violation_index(assessor.findings())

    registry.ledger.apply_batch([{"op": "delete", "table": "order", "id": "o9"}])
    assessor.advance()
    reopened = assessor.findings()
    assert ("s9",) in violation_index(reopened)
    proof = reopened["proofs"][violation_index(reopened)[("s9",)]["proofs"][0]]
    assert any(s["kind"] == "negation" for s in proof["support"])
    assert_engines_agree(registry, assessor, reopened)


def test_recursive_self_support_cycle_terminates_and_supports_alarm():
    registry, rv = make_registry()
    registry.ledger.apply_batch([
        {"op": "add", "table": "depends", "id": ("x", "y"),
         "values": {"a": "x", "b": "y"}},
        {"op": "add", "table": "depends", "id": ("y", "x"),
         "values": {"a": "y", "b": "x"}},
        {"op": "add", "table": "depends", "id": ("x", "z"),
         "values": {"a": "x", "b": "z"}},
        {"op": "add", "table": "blocked", "id": "z",
         "values": {"oid": "z"}},
    ])
    assessor = registry.assessor(rv)
    assessor.advance()
    result = assessor.findings()
    ids = violation_index(result)
    assert set(ids) == {("x",), ("y",)}
    # Cutting one edge of the cycle removes exactly the derivation that needed it.
    registry.ledger.apply_batch([
        {"op": "delete", "table": "depends", "id": ("y", "x")}])
    assessor.advance()
    ids = violation_index(assessor.findings())
    assert ("x",) in ids and ("y",) not in ids
    assert_engines_agree(registry, assessor, assessor.findings())


def test_failed_batch_exposes_no_half_conclusion():
    registry, rv = make_registry()
    assessor = registry.assessor(rv)
    assessor.advance()
    empty = assessor.findings()
    assert empty["violations"] == []
    with pytest.raises(Q.QualityError):
        registry.ledger.apply_batch([
            {"op": "delete", "table": "shipment", "id": "ghost"}])
    assert registry.ledger.version == 0
    with pytest.raises(Q.QualityError):
        registry.ledger.apply_batch([
            {"op": "add", "table": "shipment", "id": "s1",
             "values": {"sid": "s1"}}])  # missing column oid
    assert registry.ledger.version == 0
    # A batch that fails on its second op must roll back its first op too.
    with pytest.raises(Q.QualityError):
        registry.ledger.apply_batch([
            {"op": "add", "table": "order", "id": "o1",
             "values": {"oid": "o1", "status": "ok"}},
            {"op": "add", "table": "order", "id": "o1",
             "values": {"oid": "o1", "status": "ok"}}])
    assert registry.ledger.version == 0
    assert assessor.findings()["violations"] == []


def test_deleting_a_missing_row_is_rejected_not_silently_skipped():
    registry, rv = make_registry()
    registry.ledger.apply_batch([
        {"op": "add", "table": "blocked", "id": "o1",
         "values": {"oid": "o1"}}])
    version = registry.ledger.version
    with pytest.raises(Q.QualityError):
        registry.ledger.apply_batch([
            {"op": "delete", "table": "blocked", "id": "missing"}])
    assert registry.ledger.version == version


def test_budget_interruption_never_counts_unfound_violations_as_absent():
    registry, rv = make_registry()
    registry.ledger.apply_batch([
        {"op": "add", "table": "shipment", "id": "s1",
         "values": {"sid": "s1", "oid": "o1"}},
        {"op": "add", "table": "audit_flag", "id": "s1",
         "values": {"sid": "s1", "reason": "manual"}},
    ])
    assessor = registry.assessor(rv, budget=1_000)
    assessor.advance()
    consistent_version = registry.ledger.version
    batch = [
        {"op": "add", "table": "depends", "id": (f"n{i}", f"m{i}"),
         "values": {"a": f"n{i}", "b": f"m{i}"}}
        for i in range(40)]
    registry.ledger.apply_batch(batch)
    assessor.budget = assessor.work + 3
    with pytest.raises(Q.BudgetExhausted) as exc:
        assessor.advance()
    assert exc.value.partial["status"] == "incomplete"
    # Durable state remains on the last fully evaluated version.
    assert assessor.fact_version == consistent_version
    with pytest.raises(Q.QualityError):
        assessor.findings()
    with pytest.raises(Q.QualityError):
        registry.publish(rv, assessor)
    # A fresh assessor with an adequate budget completes normally.
    healthy = registry.assessor(rv)
    healthy.advance()
    published = registry.publish(rv, healthy)
    assert published["status"] == "complete"
    assert ("s1",) in violation_index(published)


def test_rule_and_result_versions_cannot_be_mixed():
    registry, rv = make_registry()
    assert rv.startswith("rv-")
    # Identical rule content hashes to the same version.
    assert registry.register_rules([dict(r) for r in RULES]) == rv
    other_registry = Q.QualityRegistry(SCHEMA)
    with pytest.raises(Q.QualityError):
        other_registry.assessor(rv)
    assessor = registry.assessor(rv)
    assessor.advance()
    # Stale assessor (ledger moved on) cannot be published.
    registry.ledger.apply_batch([
        {"op": "add", "table": "shipment", "id": "s1",
         "values": {"sid": "s1", "oid": "o1"}}])
    with pytest.raises(Q.QualityError):
        registry.publish(rv, assessor)
    # A second rule version is an independent immutable line of evaluation.
    changed = [r for r in RULES if r["id"] != "flagged_shipment"]
    rv2 = registry.register_rules(changed)
    assert rv2 != rv
    fresh = registry.assessor(rv2)
    fresh.advance()
    result2 = registry.publish(rv2, fresh)
    assert result2["rule_version"] == rv2
    # The two rule versions are independent published lines; no cross-mixing.
    assert (rv, 1) not in registry.published
    assert registry.published[(rv2, 1)]["rule_version"] == rv2
    with pytest.raises(Q.QualityError):
        registry.publish(rv, fresh)  # rule version mismatch


def test_unstratified_unsafe_and_malformed_programs_are_rejected():
    with pytest.raises(Q.QualityError):
        Q.QualityAssessor([
            {"id": "loop", "head": ["p", "?x"],
             "body": [["domain", "?x"]], "not": [["p", "?x"]]}],
            Q.FactLedger(SCHEMA))
    with pytest.raises(Q.QualityError):
        Q.QualityAssessor([
            {"id": "unsafe", "head": ["violation", "?x"],
             "body": [["shipment", "?s", "?o"]]}], Q.FactLedger(SCHEMA))
    with pytest.raises(Q.QualityError):
        Q.QualityAssessor([
            {"id": "noviolation", "head": ["p", "?x"],
             "body": [["blocked", "?x"]]}], Q.FactLedger(SCHEMA))
    with pytest.raises(Q.QualityError):
        Q.QualityRegistry({"tables": []})
    with pytest.raises(Q.QualityError):
        Q.QualityRegistry({"tables": [
            {"name": "t", "columns": ["c"], "key": ["missing"]}]})


def test_random_interleaved_batches_match_full_recompute_every_step():
    rng = random.Random(20261119)
    registry, rv = make_registry()
    assessor = registry.assessor(rv)
    assessor.advance()
    assert_engines_agree(registry, assessor, assessor.findings())

    shipments = [f"s{i}" for i in range(6)]
    orders = [f"o{i}" for i in range(6)]
    live = {"shipment": set(), "order": set(), "audit_flag": set(),
            "depends": set(), "blocked": set()}
    for step in range(60):
        ops = []
        batch_live = {table: set(keys) for table, keys in live.items()}
        for _ in range(rng.randint(1, 4)):
            table = rng.choice(list(live))
            deleting = batch_live[table] and rng.random() < 0.4
            if deleting:
                key = rng.choice(sorted(batch_live[table]))
                ops.append({"op": "delete", "table": table, "id": key_id(key)})
                batch_live[table].discard(key)
                live[table].discard(key)
            else:
                if table == "shipment":
                    sid = rng.choice(shipments)
                    if sid in batch_live[table]:
                        continue
                    oid = rng.choice(orders)
                    ops.append({"op": "add", "table": table, "id": sid,
                                "values": {"sid": sid, "oid": oid}})
                    batch_live[table].add(sid)
                    live[table].add(sid)
                elif table == "order":
                    oid = rng.choice(orders)
                    if oid in batch_live[table]:
                        continue
                    status = rng.choice(["ok", "cancelled"])
                    ops.append({"op": "add", "table": table, "id": oid,
                                "values": {"oid": oid, "status": status}})
                    batch_live[table].add(oid)
                    live[table].add(oid)
                elif table == "audit_flag":
                    sid = rng.choice(shipments)
                    if sid in batch_live[table]:
                        continue
                    ops.append({"op": "add", "table": table, "id": sid,
                                "values": {"sid": sid, "reason": "auto"}})
                    batch_live[table].add(sid)
                    live[table].add(sid)
                elif table == "depends":
                    a, b = rng.sample(orders, 2)
                    if (a, b) in batch_live[table]:
                        continue
                    ops.append({"op": "add", "table": table, "id": [a, b],
                                "values": {"a": a, "b": b}})
                    batch_live[table].add((a, b))
                    live[table].add((a, b))
                else:
                    oid = rng.choice(orders)
                    if oid in batch_live[table]:
                        continue
                    ops.append({"op": "add", "table": table, "id": oid,
                                "values": {"oid": oid}})
                    batch_live[table].add(oid)
                    live[table].add(oid)
        if not ops:
            continue
        registry.ledger.apply_batch(ops)
        assessor.advance()
        result = assessor.findings()
        assert result["fact_version"] == registry.ledger.version
        assert_engines_agree(registry, assessor, result)


def test_composite_key_tables_join_through_stable_identity():
    schema = {"tables": [
        {"name": "line", "columns": ["order_id", "line_no", "amount"],
         "key": ["order_id", "line_no"]},
        {"name": "header", "columns": ["order_id", "state"], "key": ["order_id"]},
    ]}
    rules = [
        {"id": "line_header", "head": ["line_header", "?oid", "?ln", "?st"],
         "body": [["line", "?oid", "?ln", "?amount"],
                  ["header", "?oid", "?st"]]},
        {"id": "header_exists", "head": ["header_exists", "?oid"],
         "body": [["header", "?oid", "?st"]]},
        {"id": "orphan_line", "head": ["violation", "?oid", "?ln"],
         "body": [["line", "?oid", "?ln", "?amount"]],
         "not": [["header_exists", "?oid"]]},
    ]
    registry = Q.QualityRegistry(schema)
    rv = registry.register_rules(rules)
    registry.ledger.apply_batch([
        {"op": "add", "table": "line", "id": ["o1", 1],
         "values": {"order_id": "o1", "line_no": 1, "amount": 9}},
        {"op": "add", "table": "line", "id": ["o2", 7],
         "values": {"order_id": "o2", "line_no": 7, "amount": 4}},
        {"op": "add", "table": "header", "id": "o1",
         "values": {"order_id": "o1", "state": "open"}},
    ])
    assessor = registry.assessor(rv)
    assessor.advance()
    result = assessor.findings()
    ids = {tuple(v["identity"]) for v in result["violations"]}
    assert ids == {("o2", 7)}
    orphan = result["violations"][0]
    assert orphan["affected_rows"] == [
        {"table": "line", "id": ["o2", 7]}]
    # Adding the missing header flips the negation and clears the violation.
    registry.ledger.apply_batch([
        {"op": "add", "table": "header", "id": "o2",
         "values": {"order_id": "o2", "state": "closed"}}])
    assessor.advance()
    assert assessor.findings()["violations"] == []
    # Independent full recompute reaches the same conclusions.
    report = R.summary(schema, _ref_rows(registry.ledger, schema), rules)
    assert report["violations"] == []


def _ref_rows(ledger, schema):
    rows = {}
    for table, store in ledger.snapshot().items():
        columns = ledger.tables[table]["columns"]
        rows[table] = [(list(kid), dict(zip(columns, values)))
                       for kid, values in store.items()]
    return rows


def test_reference_engine_budget_interruption_is_not_a_clean_verdict():
    schema = {"tables": [{"name": "t", "columns": ["k", "v"], "key": ["k"]}]}
    rules = [{"id": "r", "head": ["violation", "?k"],
              "body": [["t", "?k", "?v"]]}]
    rows = {"t": [(("k%d" % i,), {"k": "k%d" % i, "v": i}) for i in range(50)]}
    report = R.summary(schema, rows, rules)
    assert len(report["violations"]) == 50
    with pytest.raises(R.ReferenceBudget):
        R.evaluate_full(schema, rows, rules, budget=1)


def test_api_quality_operation_reports_complete_incomplete_and_rejected():
    request = dict(api.EXAMPLES["quality"])
    response = api.quality_call(request)
    assert response["rule_version"].startswith("rv-")
    statuses = [run["status"] for run in response["runs"]]
    assert statuses == ["complete", "complete", "complete"]
    first, second, third = response["runs"]
    assert {tuple(v["identity"]) for v in first["violations"]} == {("s1",)}
    # Order appears -> negation flips; audit flag still backs the alarm.
    assert {tuple(v["identity"]) for v in second["violations"]} == {("s1",)}
    # Audit flag withdrawn -> the alarm has no basis left.
    assert third["violations"] == []
    # Rejected and interrupted batches are reported, never silently completed.
    rejected = api.quality_call({
        "schema": request["schema"], "rules": request["rules"],
        "batches": [[{"op": "delete", "table": "order", "id": "ghost"}]]})
    assert rejected["runs"][0]["status"] == "rejected_batch"
    interrupted = api.quality_call({
        "schema": request["schema"], "rules": request["rules"],
        "budget": 1,
        "batches": [[{"op": "add", "table": "order", "id": "o1",
                      "values": {"oid": "o1", "status": "ok"}}]]})
    assert interrupted["runs"][0]["status"] == "incomplete"
    # The full operation is JSON-serializable for the HTTP layer.
    json.dumps(response)
