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
| `documentconvert` | Chunked streaming conversion of JSON documents into flat records with a declared path-to-field schema, explicit zip/separate array expansion and locatable rejections. |
| `datalog` | Finite positive recursion and stratified negation with safety validation and a work budget. |
| `boolean` | Ordered reduced decision diagrams, logical operations, existential quantification and witnesses. |

## Request examples

Run `python -m collationkey.examples` to print valid JSON requests. The output is generated locally and contains no credentials.

```sh
./.venv/bin/python -m collationkey.examples > requests.json
curl -s http://127.0.0.1:8080/capabilities
```

Use one operation request from that output as the JSON body for its `/v1/<operation>` endpoint. The `demo` command executes all five operations and prints their computed results.

## Supported scope

ICU is a system dependency: macOS libicucore or Linux ICU i18n. Profiles report ICU fallback warnings and keys with different complete profiles are rejected. Regex supports no backreferences, lookaround, character classes or capture extraction. Document selection uses a bounded DOM, not a streaming parser; duplicate object keys are rejected. Document conversion accepts the source in chunks with bounded parse stack, mapping state, capture work and staging queues; sibling arrays expand only as declared (zip alignment with per-frame cardinality checks, or separate record streams), never as a silent Cartesian product. Records keep null and missing distinct, and output produced before a successful finish is candidate data that any document failure (truncation, malformed input, duplicate keys, budget excess) revokes with a locatable error. Datalog uses ground scalar facts, safe finite rules and full recomputation for fact changes. Boolean node IDs belong to their creating manager and must never be mixed across managers.

Runtime state uses explicit temporary directories or caller-provided library objects. Service examples are per-request computations; use the library interfaces for persistent state and long-lived readers.

## Dependencies

Pinned Python dependencies are listed in `requirements.txt`; the development test runner is pinned in `requirements-dev.txt`. System libraries or a native compiler, when required above, are detected at runtime. No accounts, network connection strings, tokens or private-key files are required.
