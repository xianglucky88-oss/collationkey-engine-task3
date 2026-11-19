"""Independent full-recompute reference evaluator for small quality tables.

This module deliberately shares no implementation with ``quality``: it
re-parses rules, re-derives the stratification and re-evaluates the whole
program from a complete row snapshot on every call. It exists so tests can
cross-check the incremental engine after interleaved batches of additions
and deletions. If the two implementations disagree about either the
violations or the canonical shape of their provenance, one of them is wrong.

Provenance is reported as canonical, version-free proof fingerprints:

* ``("fact", table, identity_tuple)``
* ``("rule", rule_id, head_tuple, (fingerprint, ...))``
* ``("neg", predicate, term_tuple)``

The fingerprint forest contains *every* independent derivation of a tuple,
so a violation whose original row was withdrawn still exposes the second
basis while it survives.
"""
from collections import defaultdict

DEFAULT_BUDGET = 100_000


class ReferenceBudget(RuntimeError):
    pass


def _isvar(term):
    return isinstance(term, str) and term.startswith("?")


def _vars(atom):
    return {t for t in atom[1:] if _isvar(t)}


def _prepare(schema, rows, rules):
    table_arities = {}
    keyed = {}
    for table in schema["tables"]:
        name = table["name"]
        non_key = [c for c in table["columns"] if c not in table["key"]]
        table_arities[name] = len(table["key"]) + len(non_key)
        keyed[name] = (table["key"], table["columns"])
    relations = {name: {} for name in table_arities}  # tuple -> set of fingerprints
    work = [0]
    for name, entries in rows.items():
        if name not in relations:
            raise ValueError("row for unknown table %s" % name)
        key_cols, columns = keyed[name]
        for identity, values in entries:
            if set(values) != set(columns):
                raise ValueError("row for %s must provide exactly its columns" % name)
            if tuple(identity) != tuple(values[c] for c in key_cols):
                raise ValueError("row identity in %s must equal key columns" % name)
            non_key_values = [values[c] for c in columns if c not in key_cols]
            value_tuple = tuple([*identity, *non_key_values])
            relations[name].setdefault(value_tuple, set()).add(
                ("fact", name, tuple(identity)))
    arity = dict(table_arities)
    parsed = []
    seen = set()
    for rule in rules:
        if rule["id"] in seen:
            raise ValueError("duplicate rule id")
        seen.add(rule["id"])
        head = tuple(rule["head"])
        body = [tuple(a) for a in rule.get("body", [])]
        neg = [tuple(a) for a in rule.get("not", [])]
        for atom in (head, *body, *neg):
            width = len(atom) - 1
            if atom[0] in arity and arity[atom[0]] != width:
                raise ValueError("arity mismatch")
            arity[atom[0]] = width
        bound = set().union(*(_vars(a) for a in body)) if body else set()
        if not body or not _vars(head) <= bound or any(not _vars(a) <= bound for a in neg):
            raise ValueError("unsafe rule or empty positive body")
        parsed.append({"id": rule["id"], "head": head, "body": body, "not": neg})
    if "violation" not in arity or arity["violation"] < 1:
        raise ValueError("a violation rule with an identity term is required")
    strata = {p: 0 for p in arity}
    for _ in range(len(strata) + 1):
        changed = False
        for rule in parsed:
            h = rule["head"][0]
            for atom in rule["body"]:
                if strata[h] < strata[atom[0]]:
                    strata[h] = strata[atom[0]]
                    changed = True
            for atom in rule["not"]:
                if strata[h] < strata[atom[0]] + 1:
                    strata[h] = strata[atom[0]] + 1
                    changed = True
        if not changed:
            break
    else:
        raise ValueError("unstratified negation")
    return relations, parsed, strata, work


def _match(atom, store, envs, work, budget):
    expanded = []
    for env in envs:
        for value_tuple, fingerprints in store.items():
            work[0] += 1
            if work[0] > budget:
                raise ReferenceBudget("reference work budget exceeded")
            new_env = dict(env)
            ok = True
            for term, value in zip(atom[1:], value_tuple):
                if _isvar(term):
                    if term in new_env and new_env[term] != value:
                        ok = False
                        break
                    new_env[term] = value
                elif term != value:
                    ok = False
                    break
            if ok:
                expanded.append(new_env)
    return expanded


def evaluate_full(schema, rows, rules, budget=DEFAULT_BUDGET):
    """Evaluate a complete snapshot from scratch.

    Returns ``(violation_map, relations)`` where each predicate maps value
    tuples to a set of proof fingerprints. Fingerprints are memoized by
    ``(rule id, head tuple, value-level support)`` so recursive cycles share
    references instead of unrolling into infinite trees. Raises
    ReferenceBudget if the budget is exhausted before a fixed point.
    """
    relations, parsed, strata, work = _prepare(schema, rows, rules)
    memo = {}
    by_level = defaultdict(list)
    for rule in parsed:
        by_level[strata[rule["head"][0]]].append(rule)
        relations.setdefault(rule["head"][0], {})
    for level in sorted(set(strata.values())):
        changed = True
        while changed:
            changed = False
            for rule in by_level.get(level, []):
                candidates = [{}]
                for atom in rule["body"]:
                    candidates = _match(atom, relations.get(atom[0], {}),
                                        candidates, work, budget)
                for env in candidates:
                    def ground(atom):
                        return tuple(env[t] if _isvar(t) else t for t in atom[1:])
                    support = []
                    support_values = []
                    ok = True
                    for atom in rule["body"]:
                        value_tuple = ground(atom)
                        work[0] += 1
                        fingerprints = relations.get(atom[0], {}).get(value_tuple)
                        if not fingerprints:
                            ok = False
                            break
                        support.append(tuple(sorted(fingerprints, key=repr)))
                        support_values.append(("a", atom[0], value_tuple))
                    if not ok:
                        continue
                    for atom in rule["not"]:
                        value_tuple = ground(atom)
                        work[0] += 1
                        if value_tuple in relations.get(atom[0], {}):
                            ok = False
                            break
                        support.append(("neg", atom[0], value_tuple))
                        support_values.append(("n", atom[0], value_tuple))
                    if not ok:
                        continue
                    head_tuple = ground(rule["head"])
                    fp_key = (rule["id"], head_tuple, tuple(support_values))
                    fingerprint = memo.get(fp_key)
                    if fingerprint is None:
                        fingerprint = ("rule", rule["id"], head_tuple, tuple(support))
                        memo[fp_key] = fingerprint
                    target = relations[rule["head"][0]].setdefault(head_tuple, set())
                    if fingerprint not in target:
                        target.add(fingerprint)
                        changed = True
                        work[0] += 1
                        if work[0] > budget:
                            raise ReferenceBudget("reference work budget exceeded")
    return relations["violation"], relations


def root_facts(fingerprint, relations, seen=None):
    """Ground facts supporting one proof, path-sensitive across cycles.

    Recursive self-support cycles are cut on the current DFS path only: a
    cycle node is still explored independently from every other derivation
    that reaches it, so facts belonging to an unrelated cycle are never
    attributed to a violation whose derivation merely shares a node.
    """
    if fingerprint[0] == "fact":
        return {fingerprint}
    if fingerprint[0] == "neg":
        return set()
    if seen is None:
        seen = frozenset()
    marker = id(fingerprint)
    if marker in seen:
        return set()
    seen = seen | {marker}
    roots = set()
    for support in fingerprint[3]:
        if support[0] == "neg":
            continue
        for child in support:
            roots |= root_facts(child, relations, seen)
    return roots


def summary(schema, rows, rules, budget=DEFAULT_BUDGET):
    """JSON-friendly snapshot used by tests and the demo cross-check."""
    violation_map, relations = evaluate_full(schema, rows, rules, budget)
    out = []
    for head_tuple in sorted(violation_map, key=repr):
        fingerprints = sorted(violation_map[head_tuple], key=repr)
        facts = set()
        for fingerprint in fingerprints:
            facts |= root_facts(fingerprint, relations)
        out.append({
            "identity": list(head_tuple),
            "support_count": len(fingerprints),
            "fact_bases": sorted(([table, list(identity)]
                                  for _, table, identity in facts), key=repr),
        })
    return {"status": "complete", "violations": out}
