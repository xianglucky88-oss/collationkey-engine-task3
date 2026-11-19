"""Versioned cross-table quality evaluation.

The caller supplies table schemas, stable row identities, batches of row
additions/deletions and stratified quality rules written over row facts.
Every derived violation carries a reviewable proof: either a ground row fact
pinned to a fact version, or a rule firing whose support links back to more
proofs (including negation strata that flipped because a fact appeared or
disappeared).

Versioning is explicit and never implicit:

* fact versions advance one tick per successfully applied batch; a failed
  batch changes nothing and exposes no half conclusion;
* rule versions are content hashes of an immutable normalized rule set;
* published quality results pin both versions plus the rule set version of
  every supporting proof, so rule/fact/result versions can never be mixed.

Evaluation is incremental across batches but recomputes whole strata, which
keeps recursive self-support cycles and negation flips honest: a violation
stays open while any independent proof survives and is withdrawn only when
every proof collapses. A work-budget interruption raises BudgetExhausted;
violations not yet reached are never reported as absent, durable state keeps
its last consistent value and nothing incomplete can be published.
"""
from collections import defaultdict
import copy
import hashlib
import json

SCALAR = (str, int, bool, float)
VIOLATION_PREDICATE = "violation"
DEFAULT_BUDGET = 100_000


class QualityError(ValueError):
    """Invalid schema, rule set, batch operation or version reference."""


class BudgetExhausted(QualityError):
    """The work budget ran out before evaluation could reach a fixed point.

    ``partial`` describes what the interrupted pass had found so far; it is
    provisional evidence only, never a complete compliance conclusion. The
    assessor's durable state remains at its last consistent fact version.
    """

    def __init__(self, message, partial):
        super().__init__(message)
        self.partial = partial


def isvar(term):
    return isinstance(term, str) and term.startswith("?")


def variables(atom):
    return {term for term in atom[1:] if isvar(term)}


def key_tuple(key):
    if isinstance(key, (list, tuple)):
        return tuple(key)
    return (key,)


def _scalar(value, what):
    if not isinstance(value, SCALAR):
        raise QualityError("%s must be a ground scalar" % what)
    return value


def _check_atom(atom, arity):
    if not isinstance(atom, (list, tuple)) or not atom or not isinstance(atom[0], str):
        raise QualityError("invalid atom")
    name = atom[0]
    width = len(atom) - 1
    if name in arity and arity[name] != width:
        raise QualityError("arity mismatch for predicate %s" % name)
    for term in atom[1:]:
        if not isvar(term) and not isinstance(term, SCALAR):
            raise QualityError("atom terms must be variables or scalars")
    arity[name] = width
    return name


def normalize_rules(rules, table_arities):
    """Validate and deep-copy rules into a canonical, hashable structure."""
    if not isinstance(rules, (list, tuple)) or not rules:
        raise QualityError("at least one rule is required")
    arity = dict(table_arities)
    normalized = []
    seen_ids = set()
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule["id"]:
            raise QualityError("every rule needs a string id")
        if rule["id"] in seen_ids:
            raise QualityError("duplicate rule id %s" % rule["id"])
        seen_ids.add(rule["id"])
        head = tuple(rule.get("head", ()))
        _check_atom(head, arity)
        positives = [tuple(a) for a in rule.get("body", [])]
        negatives = [tuple(a) for a in rule.get("not", [])]
        for atom in positives + negatives:
            _check_atom(atom, arity)
        bound = set()
        for atom in positives:
            bound |= variables(atom)
        if not variables(head) <= bound:
            raise QualityError("unsafe head variable in rule %s" % rule["id"])
        for atom in negatives:
            if not variables(atom) <= bound:
                raise QualityError("unsafe negated variable in rule %s" % rule["id"])
        if not positives:
            raise QualityError("rule %s needs at least one positive body atom" % rule["id"])
        normalized.append({"id": rule["id"], "head": head,
                           "body": tuple(positives), "not": tuple(negatives)})
    if VIOLATION_PREDICATE not in arity:
        raise QualityError("a rule deriving the %r predicate is required" % VIOLATION_PREDICATE)
    if arity[VIOLATION_PREDICATE] < 1:
        raise QualityError("violation atoms must carry at least a row identity term")
    return normalized, arity


def rule_version_hash(normalized):
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "rv-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def stratify(normalized, base_predicates):
    """Return predicate -> stratum level; raise on unstratified negation."""
    strata = {p: 0 for p in base_predicates}
    for rule in normalized:
        strata.setdefault(rule["head"][0], 0)
        for atom in rule["body"] + rule["not"]:
            strata.setdefault(atom[0], 0)
    for _ in range(len(strata) + 1):
        changed = False
        for rule in normalized:
            head = rule["head"][0]
            for atom in rule["body"]:
                if strata[head] < strata[atom[0]]:
                    strata[head] = strata[atom[0]]
                    changed = True
            for atom in rule["not"]:
                need = strata[atom[0]] + 1
                if strata[head] < need:
                    strata[head] = need
                    changed = True
        if not changed:
            return strata
    raise QualityError("unstratified negation in rule set")


class FactLedger:
    """Append-only versioned store of row facts.

    Rows are addressed by stable caller-provided identities; physical row
    order never matters. Each successful batch ticks ``version`` and records
    which table predicates changed, which drives incremental stratum
    recomputation. A batch that fails validation applies nothing.
    """

    def __init__(self, schema):
        if not isinstance(schema, dict) or not isinstance(schema.get("tables"), list) or not schema["tables"]:
            raise QualityError("schema needs a non-empty tables list")
        self.tables = {}
        for table in schema["tables"]:
            if not isinstance(table, dict) or not isinstance(table.get("name"), str):
                raise QualityError("each table needs a name")
            name = table["name"]
            if name in self.tables:
                raise QualityError("duplicate table %s" % name)
            columns = table.get("columns")
            key = table.get("key")
            if not isinstance(columns, list) or not columns or any(not isinstance(c, str) for c in columns):
                raise QualityError("table %s needs string columns" % name)
            if not isinstance(key, list) or not key or any(k not in columns for k in key):
                raise QualityError("table %s key must be a non-empty subset of columns" % name)
            if len(set(columns)) != len(columns):
                raise QualityError("duplicate column in table %s" % name)
            self.tables[name] = {"columns": tuple(columns), "key": tuple(key)}
        # rows[table] = {identity tuple: column-value tuple}
        self.rows = {name: {} for name in self.tables}
        self.version = 0
        self.changes = []  # changes[v-1] = tables touched by the batch creating v

    def _row(self, table, row_id, values):
        spec = self.tables[table]
        if not isinstance(values, dict) or set(values) != set(spec["columns"]):
            raise QualityError("row for %s must provide exactly columns %s" % (table, spec["columns"]))
        for column, value in values.items():
            _scalar(value, "value %s.%s" % (table, column))
        kid = key_tuple(row_id)
        for part in kid:
            _scalar(part, "row id part in %s" % table)
        if len(kid) != len(spec["key"]):
            raise QualityError("row id for %s must match key width %d" % (table, len(spec["key"])))
        if list(kid) != [values[k] for k in spec["key"]]:
            raise QualityError("row id for %s must equal its key columns" % table)
        return kid, tuple(values[c] for c in spec["columns"])

    def apply_batch(self, operations):
        """Validate against a private copy, then commit one batch.

        A rejected batch leaves the ledger and every downstream conclusion
        untouched; no new fact version is minted.
        """
        if not isinstance(operations, list) or not operations:
            raise QualityError("a batch needs at least one operation")
        draft = {name: dict(store) for name, store in self.rows.items()}
        touched = set()
        for op in operations:
            if not isinstance(op, dict) or op.get("op") not in ("add", "delete"):
                raise QualityError("operation must be add or delete")
            table = op.get("table")
            if table not in self.tables:
                raise QualityError("unknown table %s" % table)
            if op["op"] == "add":
                kid, row = self._row(table, op.get("id"), op.get("values"))
                if kid in draft[table]:
                    raise QualityError("row %s already exists in %s" % (list(kid), table))
                draft[table][kid] = row
            else:
                kid = key_tuple(op.get("id"))
                if kid not in draft[table]:
                    raise QualityError("cannot delete missing row %s from %s" % (list(kid), table))
                del draft[table][kid]
            touched.add(table)
        self.rows = draft
        self.version += 1
        self.changes.append(touched)
        return self.version

    def changes_since(self, fact_version):
        touched = set()
        for changed in self.changes[fact_version:]:
            touched |= changed
        return touched

    def snapshot(self):
        return {name: dict(store) for name, store in self.rows.items()}


class QualityAssessor:
    """Incremental stratified evaluator with version-pinned provenance."""

    def __init__(self, rules, ledger, budget=DEFAULT_BUDGET):
        if not isinstance(ledger, FactLedger):
            raise QualityError("assessor requires a FactLedger")
        self.ledger = ledger
        # Table atoms read as identity terms followed by non-key column terms.
        table_arities = {
            name: len(spec["key"]) +
            len([c for c in spec["columns"] if c not in spec["key"]])
            for name, spec in ledger.tables.items()}
        self.rules, self.arity = normalize_rules(rules, table_arities)
        self.rule_version = rule_version_hash(self.rules)
        self.budget = budget
        self.strata = stratify(self.rules, set(ledger.tables))
        self.levels = sorted(set(self.strata.values()))
        self.rules_by_level = defaultdict(list)
        for rule in self.rules:
            self.rules_by_level[self.strata[rule["head"][0]]].append(rule)
        self.dependents = defaultdict(set)
        for rule in self.rules:
            for atom in rule["body"] + rule["not"]:
                self.dependents[atom[0]].add(rule["head"][0])
        # state[level][predicate] = {value tuple: proof id}
        self.state = {}
        self.fact_version = 0
        self._key_to_pid = {}
        self._pid_to_key = {}
        self._proofs = {}
        self._next_pid = 0
        self.work = 0

    # ---------------------------------------------------------------- proofs
    def _intern(self, key, payload):
        pid = self._key_to_pid.get(key)
        if pid is None:
            pid = "p%d" % self._next_pid
            self._next_pid += 1
            self._key_to_pid[key] = pid
            self._pid_to_key[pid] = key
            self._proofs[pid] = payload
        return pid

    def _fact_proof(self, table, kid):
        payload = {"kind": "fact", "table": table, "id": list(kid),
                   "fact_version": self.ledger.version,
                   "rule_version": self.rule_version}
        return self._intern(("fact", table, kid, self.ledger.version), payload)

    def _rule_proof(self, rule, head_tuple, support, support_key, alternates):
        # Proof identity includes the fact version so a firing re-derived in a
        # later batch carries fresh version stamps (notably negation flips);
        # proof ids only label one chosen chain while alternate_support
        # records every other surviving basis.
        payload = {"kind": "rule", "rule_id": rule["id"],
                   "rule_version": self.rule_version,
                   "fact_version": self.ledger.version,
                   "head": [rule["head"][0], *head_tuple], "support": support,
                   "alternate_support": alternates}
        return self._intern(
            ("rule", rule["id"], head_tuple, support_key, self.ledger.version),
            payload)

    def _charge(self, units=1):
        self.work += units
        if self.work > self.budget:
            raise BudgetExhausted(
                "rule work budget exceeded at fact version %d" % self.ledger.version,
                self._partial_findings())

    def _partial_findings(self):
        found = []
        for level_store in self.state.values():
            for proof_ids in level_store.get(VIOLATION_PREDICATE, {}).values():
                found.extend(
                    tuple(self._proofs[pid]["head"][1:]) for pid in proof_ids)
        return {"status": "incomplete", "fact_version": self.ledger.version,
                "rule_version": self.rule_version,
                "provisional_violations": sorted((list(t) for t in set(found)), key=repr),
                "work": self.work, "budget": self.budget}

    # ------------------------------------------------------------- evaluation
    def _base_store(self, table):
        # Predicate stores map value tuple -> set of independent proof ids.
        # A table tuple is the stable identity followed by non-key columns.
        spec = self.ledger.tables[table]
        key_positions = {spec["columns"].index(k) for k in spec["key"]}
        store = {}
        for kid, full_values in self.ledger.rows[table].items():
            non_key = tuple(v for i, v in enumerate(full_values) if i not in key_positions)
            store[tuple([*kid, *non_key])] = {self._fact_proof(table, kid)}
        return store

    def _eval_level(self, level, scratch):
        # Base tables only live in stratum 0; pre-seed every predicate of
        # this level so the fixpoint view always resolves the same dicts.
        for table in self.ledger.tables:
            if self.strata[table] == level:
                scratch[table] = self._base_store(table)
        for predicate, pred_level in self.strata.items():
            if pred_level == level and predicate not in scratch:
                scratch[predicate] = {}
        # Same-level predicates (e.g. recursion over a base stratum) are
        # rebuilt here too: pull in every consistent store up to this level,
        # then let the scratch version override the predicates being redone.
        view = {}
        for done_level, store in self.state.items():
            if done_level <= level:
                view.update(store)
        view.update(scratch)
        changed = True
        while changed:
            changed = False
            for rule in self.rules_by_level.get(level, []):
                envs = [{}]
                for atom in rule["body"]:
                    rows = view.get(atom[0], {})
                    expanded = []
                    for env in envs:
                        for value_tuple in rows:
                            self._charge()
                            new_env = dict(env)
                            ok = True
                            for term, value in zip(atom[1:], value_tuple):
                                if isvar(term):
                                    if term in new_env and new_env[term] != value:
                                        ok = False
                                        break
                                    new_env[term] = value
                                elif term != value:
                                    ok = False
                                    break
                            if ok:
                                expanded.append(new_env)
                    envs = expanded
                for env in envs:
                    def ground(atom):
                        return tuple(env[t] if isvar(t) else t for t in atom[1:])
                    support = []
                    support_key = []
                    alternates = {}
                    failed = False
                    for atom in rule["body"]:
                        value_tuple = ground(atom)
                        self._charge()
                        proof_ids = view.get(atom[0], {}).get(value_tuple)
                        if not proof_ids:
                            failed = True
                            break
                        ordered = sorted(proof_ids)
                        chosen = ordered[0]
                        support.append({"kind": "atom", "predicate": atom[0],
                                        "terms": list(value_tuple), "proof": chosen})
                        support_key.append(("a", atom[0], value_tuple))
                        alternates["%s|%s" % (atom[0], list(value_tuple))] = ordered
                    if failed:
                        continue
                    for atom in rule["not"]:
                        value_tuple = ground(atom)
                        self._charge()
                        if value_tuple in view.get(atom[0], {}):
                            failed = True
                            break
                        # Absence is evidence from the completed lower stratum.
                        support.append({"kind": "negation", "predicate": atom[0],
                                        "terms": list(value_tuple), "proof": None,
                                        "stratum": self.strata[atom[0]],
                                        "fact_version": self.ledger.version})
                        support_key.append(("n", atom[0], value_tuple))
                    if failed:
                        continue
                    head_tuple = ground(rule["head"])
                    name = rule["head"][0]
                    proof_id = self._rule_proof(rule, head_tuple, support,
                                                tuple(support_key), alternates)
                    bucket = scratch[name].setdefault(head_tuple, set())
                    if proof_id not in bucket:
                        bucket.add(proof_id)
                        changed = True
                        self._charge()

    def advance(self):
        """Consume every ledger batch up to its current version.

        On BudgetExhausted all candidate structures are discarded: durable
        state stays at the previous consistent fact version and no partial
        compliance conclusion is exposed from this assessor.
        """
        if self.ledger.version == self.fact_version:
            return self.fact_version
        dirty_predicates = self.ledger.changes_since(self.fact_version)
        dirty_levels = set()
        stack = [self.strata[p] for p in dirty_predicates if p in self.strata]
        while stack:
            level = stack.pop()
            if level in dirty_levels:
                continue
            dirty_levels.add(level)
            for predicate, pred_level in self.strata.items():
                if pred_level == level:
                    for dependent in self.dependents.get(predicate, ()):
                        stack.append(self.strata[dependent])
        saved = (copy.deepcopy(self.state), dict(self._proofs), dict(self._key_to_pid),
                 dict(self._pid_to_key), self._next_pid, self.work)
        try:
            for level in self.levels:
                if level not in dirty_levels:
                    continue
                scratch = {}
                self._eval_level(level, scratch)
                self.state[level] = scratch
        except BudgetExhausted:
            (self.state, self._proofs, self._key_to_pid,
             self._pid_to_key, self._next_pid, self.work) = saved
            raise
        self.fact_version = self.ledger.version
        self._gc_proofs()
        return self.fact_version

    def _gc_proofs(self):
        # Reachability starts from every proof attached to a current store
        # and walks chosen supports; orphans from superseded recomputes are
        # dropped even if they still reference withdrawn fact proofs.
        live = set()
        roots = self._live_proof_ids()
        stack = list(roots)
        while stack:
            pid = stack.pop()
            if pid in live:
                continue
            live.add(pid)
            proof = self._proofs.get(pid)
            if proof is None or proof["kind"] != "rule":
                continue
            for support in proof["support"]:
                if support["proof"] is not None and support["proof"] not in live:
                    stack.append(support["proof"])
        for pid in [p for p in self._proofs if p not in live]:
            del self._proofs[pid]
            del self._key_to_pid[self._pid_to_key.pop(pid)]

    # ---------------------------------------------------------------- output
    def _violation_store(self):
        for level in self.levels:
            store = self.state.get(level, {})
            if VIOLATION_PREDICATE in store:
                return store[VIOLATION_PREDICATE]
        return {}

    def _live_proof_ids(self):
        """Proof ids still attached to a current predicate store.

        Interned rule proofs whose firing was superseded by a recompute can
        keep dangling references to withdrawn fact proofs; they are excluded
        here so provenance review never follows an orphan into retracted data.
        """
        live = set()
        for store in self.state.values():
            for proof_sets in store.values():
                for proof_ids in proof_sets.values():
                    live.update(proof_ids)
        return live

    def _affected_rows(self, proof_id, live, active=None):
        """Ground rows supporting a live proof, path-sensitive across cycles.

        Recursive self-support cycles are cut on the current DFS path only;
        every independent basis in alternate_support is still explored, but
        only while it remains attached to the current derivation graph.
        """
        if active is None:
            active = frozenset()
        if proof_id in active or proof_id not in live:
            return set()
        active = active | {proof_id}
        proof = self._proofs[proof_id]
        if proof["kind"] == "fact":
            return {(proof["table"], tuple(proof["id"]))}
        rows = set()
        for support in proof["support"]:
            if support["proof"] is None:
                continue
            alternatives = proof["alternate_support"].get(
                "%s|%s" % (support["predicate"], support["terms"]),
                [support["proof"]])
            for candidate in alternatives:
                rows |= self._affected_rows(candidate, live, active)
        return rows

    def findings(self):
        """Complete findings at the currently reflected fact version."""
        if self.fact_version != self.ledger.version:
            raise QualityError("assessor is behind the ledger; call advance() first")
        violations = []
        live = self._live_proof_ids()
        store = self._violation_store()
        for head_tuple in sorted(store, key=repr):
            proof_ids = sorted(store[head_tuple])
            bases = []
            seen_bases = set()
            for proof_id in proof_ids:
                for table, kid in self._affected_rows(proof_id, live):
                    basis = (table, kid)
                    if basis not in seen_bases:
                        seen_bases.add(basis)
                        bases.append({"table": table, "id": list(kid)})
            violations.append({
                "rule_id": self._proofs[proof_ids[0]]["rule_id"],
                "identity": list(head_tuple),
                "proofs": proof_ids,
                "support_count": len(proof_ids),
                "affected_rows": sorted(bases, key=repr),
            })
        return {
            "status": "complete",
            "rule_version": self.rule_version,
            "fact_version": self.fact_version,
            "violations": violations,
            "proofs": copy.deepcopy(self._proofs),
            "work": self.work,
            "budget": self.budget,
        }


class QualityRegistry:
    """Owns the fact ledger, immutable rule versions and published results."""

    def __init__(self, schema):
        self.ledger = FactLedger(schema)
        self._rule_sets = {}
        self.published = {}

    def register_rules(self, rules):
        probe = QualityAssessor(rules, self.ledger)
        version = probe.rule_version
        if version not in self._rule_sets:
            self._rule_sets[version] = probe.rules
        return version

    def assessor(self, rule_version, budget=DEFAULT_BUDGET):
        if rule_version not in self._rule_sets:
            raise QualityError("unknown rule version %s" % rule_version)
        return QualityAssessor(self._rule_sets[rule_version], self.ledger, budget=budget)

    def publish(self, rule_version, assessor):
        """Pin a complete evaluation; stale or incomplete runs are refused."""
        if rule_version not in self._rule_sets:
            raise QualityError("unknown rule version %s" % rule_version)
        if assessor.rule_version != rule_version:
            raise QualityError("rule version mismatch: refusing to mix rule sets")
        if assessor.fact_version != self.ledger.version:
            raise QualityError("stale assessor: fact versions must not be mixed")
        result = assessor.findings()
        result["published"] = True
        key = (rule_version, assessor.fact_version)
        self.published[key] = result
        return result

    def rule_versions(self):
        return sorted(self._rule_sets)
