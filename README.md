# collationkey-engine

Data-language and symbolic components.

Python 3.11 or newer. The primary interfaces are importable component libraries and a local JSON HTTP service.

## Install

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/python -m pip install --no-deps -e .
```

## Run

```sh
./.venv/bin/python -m collationkey demo
./.venv/bin/python -m collationkey serve --port 8080
./.venv/bin/python -m pytest -q
```

The server binds only to `127.0.0.1`. `GET /health` reports process readiness; `GET /capabilities` lists operations.
Each component has `POST /v1/<operation>` accepting its JSON request object. Responses contain `result` or an explicit error. The service accepts bodies up to 1 MB and at most four concurrent operation handlers. It does not expose a shell, arbitrary file paths or remote URLs.

## Components

| Operation | Behavior |
|---|---|
| `collation` | Actual system ICU comparison and sort keys with locale, strength and ICU collation-version tags. |
| `regex` | A restricted Thompson NFA with alternatives, groups, dot and quantifiers, including chunked full matching. |
| `documentpath` | A bounded JSONPath subset with object fields, array indexes and wildcards. |
| `datalog` | Finite positive recursion and stratified negation with safety validation and a work budget. |
| `boolean` | Ordered reduced decision diagrams, logical operations, existential quantification and witnesses. |
| `quality` | Versioned cross-table quality evaluation over caller-supplied row facts, with stable row identities, batch add/delete, stratified rules, reviewable provenance and published results. |

## Request examples

Run `python -m collationkey.examples` to print valid JSON requests. The output is generated locally and contains no credentials.

```sh
./.venv/bin/python -m collationkey.examples > requests.json
curl -s http://127.0.0.1:8080/capabilities
```

Use one operation request from that output as the JSON body for its `/v1/<operation>` endpoint. The `demo` command executes all five operations and prints their computed results.

## Supported scope

ICU is a system dependency: macOS libicucore or Linux ICU i18n. Profiles report ICU fallback warnings and keys with different complete profiles are rejected. Regex supports no backreferences, lookaround, character classes or capture extraction. Document selection uses a bounded DOM, not a streaming parser; duplicate object keys are rejected. Datalog uses ground scalar facts, safe finite rules and full recomputation for fact changes. Boolean node IDs belong to their creating manager and must never be mixed across managers.

## Versioned cross-table quality

The `quality` component evaluates data-quality rules across tables whose rows carry caller-provided stable identities. Raw relation data is supplied directly in the request; there is no JSON/CSV importer, file path or remote source. A rule set is normalized and content-hashed into an immutable **rule version** (`rv-…`); each successfully applied batch of additions/deletions advances the integer **fact version** by one. Table atoms read as identity terms followed by non-key column terms, and a row identity must equal its key columns.

Each batch is validated against a private copy before commit, so a rejected batch (missing row on delete, duplicate row on add, malformed columns) mints no fact version and exposes no half conclusion. Evaluation is incremental but recomposes whole strata, which keeps three mechanisms jointly deciding whether an alarm holds:

1. **Batch adds and deletes** only mark the affected strata and their dependents dirty.
2. **Recursive self-support cycles** terminate through stratified fixpoint evaluation; a violation stays open while any independent proof survives and disappears only when every proof collapses, so withdrawing one record does not silence an alarm backed by a second basis.
3. **Negation-stratum flips** re-derive the higher stratum whenever the absent predicate appears or disappears in a later batch.

Every violation lists its rule id, identity terms, all independent proof ids (`support_count`), the ground affected rows and a full version-pinned proof forest. Negation supports record the stratum and fact version where absence was observed; rule proofs carry their rule and fact versions.

Judgments are explicitly three-way so consumers can tell finished, defective and unfinished work apart:

* `complete` — the fixpoint finished within budget; the violation list is authoritative, including an empty list.
* `incomplete` — the work budget was exhausted mid-evaluation. Violations not yet reached are never reported as absent. Only `provisional_violations` seen so far are returned, durable state stays at the previous consistent fact version, and the result cannot be published. Retrying with an adequate budget on a fresh assessor yields a normal complete result.
* `rejected_batch` — the batch failed validation and changed no state.

Rule versions, fact versions and **published** quality results are never mixed: publishing requires the assessor's rule version to match the registered line and its reflected fact version to equal the ledger head. An independent full-recompute reference evaluator (`quality_ref`) re-derives every result from a complete snapshot and is used by the test suite to cross-check violations, independent-basis counts and affected rows after randomized interleaved batches.

Runtime state uses explicit temporary directories or caller-provided library objects. Service examples are per-request computations; use the library interfaces for persistent state and long-lived readers.

## Dependencies

Pinned Python dependencies are listed in `requirements.txt`; the development test runner is pinned in `requirements-dev.txt`. System libraries or a native compiler, when required above, are detected at runtime. No accounts, network connection strings, tokens or private-key files are required.
