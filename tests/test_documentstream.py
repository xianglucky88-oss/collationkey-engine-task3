import json
import random
import re

import pytest

from collationkey import api, documentpath, documentstream
from collationkey.documentstream import Conversion, ConversionError


# ---------------------------------------------------------------- reference
# Independent DOM + naive field-mapping reference used to cross-check the
# streaming converter's record values, counts and source paths.

_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*$")


def unique(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ValueError("duplicate object key")
        obj[k] = v
    return obj


def render(steps):
    out = ["$"]
    for kind, arg in steps:
        if kind == "field":
            out.append("." + arg if _IDENT.match(arg)
                       else '["' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"]')
        elif kind == "index":
            out.append("[" + str(arg) + "]")
        else:
            out.append("[*]")
    return "".join(out)


def jtype(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def type_ok(value, ftype, nullable):
    if value is None:
        return nullable or ftype == "null"
    if ftype == "any":
        return True
    if ftype == "string":
        return isinstance(value, str)
    if ftype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if ftype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ftype == "boolean":
        return isinstance(value, bool)
    if ftype == "object":
        return isinstance(value, dict)
    if ftype == "array":
        return isinstance(value, list)
    return False


def analyze(spec):
    fields = []
    for item in spec["fields"]:
        steps = tuple(documentpath.parse(item["path"]))
        last = -1
        for k, s in enumerate(steps):
            if s[0] == "all":
                last = k
        fields.append({"steps": steps, "scope": steps[:last + 1], "rest": steps[last + 1:],
                       "name": item["field"], "type": item.get("type", "any"),
                       "required": bool(item.get("required", False)),
                       "nullable": bool(item.get("nullable", True))})
    scopes = sorted({f["scope"] for f in fields})
    leaves = [s for s in scopes if not any(o != s and o[:len(s)] == s for o in scopes)]
    mode = spec.get("arrays") or "separate"
    frame = None
    if mode == "zip" and len(leaves) > 1:
        locs = [leaf[:-1] for leaf in leaves]
        frame = locs[0]
        for loc in locs[1:]:
            k = 0
            while k < min(len(frame), len(loc)) and frame[k] == loc[k]:
                k += 1
            frame = frame[:k]
    return fields, leaves, mode, frame


def walk(value, steps, base):
    cur = [(base, 0, value)]
    for kind, arg in steps:
        nxt = []
        for path, _, v in cur:
            if kind == "field":
                if isinstance(v, dict) and arg in v:
                    nxt.append((path + (("field", arg),), 0, v[arg]))
            elif kind == "index":
                if isinstance(v, list) and arg < len(v):
                    nxt.append((path + (("index", arg),), arg, v[arg]))
            else:
                if isinstance(v, list):
                    nxt += [(path + (("index", i),), i, x) for i, x in enumerate(v)]
                elif isinstance(v, dict):
                    nxt += [(path + (("field", k),), i, x) for i, (k, x) in enumerate(v.items())]
        cur = nxt
    return cur


def resolve(value, steps, base):
    found = walk(value, steps, base)
    return None if not found else (found[0][0], found[0][2])


def navigate(doc, steps):
    v = doc
    for _, arg in steps:
        v = v[arg]
    return v


def ref_convert(doc, spec):
    fields, leaves, mode, frame = analyze(spec)
    records, rejections = [], []

    def build(shape, provider, path, index, stream):
        out, missing, sources, bad = {}, [], {}, []
        for f in shape:
            got = provider(f)
            if got is None:
                if f["required"]:
                    bad.append({"reason": "missing_required", "field": f["name"], "path": path})
                else:
                    missing.append(f["name"])
            else:
                value, src = got
                if type_ok(value, f["type"], f["nullable"]):
                    out[f["name"]] = value
                    sources[f["name"]] = src
                else:
                    bad.append({"reason": "type_mismatch", "field": f["name"],
                                "expected": f["type"], "actual": jtype(value),
                                "path": src, "record": path})
        if bad:
            rejections.extend(bad)
        else:
            records.append({"stream": stream, "path": path, "index": index,
                            "record": out, "missing": missing, "sources": sources})

    def scoped_provider(f, inst_steps):
        anc = inst_steps[:len(f["scope"])]
        got = resolve(navigate(doc, anc), f["rest"], anc)
        return None if got is None else (got[1], render(got[0]))

    if mode == "zip" and len(leaves) > 1:
        for fsteps, _, fval in walk(doc, frame, ()):
            elems = {}
            for leaf in leaves:
                found = resolve(fval, leaf[len(frame):-1], fsteps)
                lst = []
                if found is not None:
                    apath, aval = found
                    if isinstance(aval, list):
                        lst = [(apath + (("index", i),), i, x) for i, x in enumerate(aval)]
                    elif isinstance(aval, dict):
                        lst = [(apath + (("field", k),), i, x)
                               for i, (k, x) in enumerate(aval.items())]
                elems[leaf] = lst
            counts = {len(v) for v in elems.values()}
            if len(counts) > 1:
                rejections.append({"reason": "cardinality_mismatch", "path": render(fsteps),
                                   "arrays": {render(leaf): len(elems[leaf]) for leaf in leaves}})
                continue
            n = counts.pop() if counts else 0
            for i in range(n):
                def provider(f, i=i, fsteps=fsteps, elems=elems):
                    if f["scope"] in leaves:
                        psteps, _, elem = elems[f["scope"]][i]
                        got = resolve(elem, f["rest"], psteps)
                        return None if got is None else (got[1], render(got[0]))
                    return scoped_provider(f, fsteps)
                build(fields, provider, render(fsteps), i, render(frame))
    else:
        for leaf in leaves:
            shape = [f for f in fields if f["scope"] == leaf[:len(f["scope"])]]
            for psteps, ordinal, _ in walk(doc, leaf, ()):
                build(shape, lambda f, p=psteps: scoped_provider(f, p),
                      render(psteps), ordinal, render(leaf))
    return records, rejections


def canon_records(records):
    return sorted(
        (r["stream"], r["path"], r["index"],
         json.dumps(r["record"], sort_keys=True),
         tuple(r["missing"]),
         json.dumps(r["sources"], sort_keys=True))
        for r in records)


def canon_rejections(rejections):
    return sorted(json.dumps({k: v for k, v in r.items() if k != "offset"}, sort_keys=True)
                  for r in rejections)


def run_streaming(text, spec, sizes):
    conv = Conversion(spec)
    cands, rejs = [], []
    pos = 0
    for size in sizes:
        if pos >= len(text):
            break
        got = conv.feed(text[pos:pos + size])
        assert 0 <= got <= size
        pos += got
        out = conv.poll()
        cands += out["candidates"]
        rejs += out["rejections"]
    while pos < len(text):
        got = conv.feed(text[pos:])
        pos += got
        out = conv.poll()
        cands += out["candidates"]
        rejs += out["rejections"]
        assert got > 0 or out["candidates"] or out["rejections"]
    summary = conv.finish()
    out = conv.poll()
    cands += out["candidates"]
    rejs += out["rejections"]
    assert conv.poll() == {"candidates": [], "rejections": []}
    return cands, rejs, summary


# --------------------------------------------------------------- generators
def gen_value(rng, depth):
    r = rng.random()
    if depth <= 0 or r < 0.4:
        return rng.choice([None, True, False, 0, 1, -2, 2.5, "x", "y", ""])
    if r < 0.75:
        return {k: gen_value(rng, depth - 1) for k in rng.sample("abcd", rng.randint(1, 4))}
    return [gen_value(rng, depth - 1) for _ in range(rng.randint(0, 3))]


def gen_path(rng):
    parts = ["$"]
    for _ in range(rng.randint(1, 3)):
        r = rng.random()
        if r < 0.5:
            parts.append("." + rng.choice("abcd"))
        elif r < 0.8:
            parts.append("[*]")
        else:
            parts.append("[0]")
    return "".join(parts)


def gen_spec(rng):
    fields = []
    for i in range(rng.randint(1, 4)):
        fields.append({"path": gen_path(rng), "field": "f" + str(i),
                       "type": rng.choice(["any", "string", "integer", "number",
                                           "boolean", "object", "array", "null"]),
                       "required": rng.random() < 0.2, "nullable": rng.random() < 0.7})
    spec = {"fields": fields}
    mode = rng.choice([None, "zip", "separate"])
    if mode:
        spec["arrays"] = mode
    return spec


# ------------------------------------------------------------------- tests
def test_null_and_missing_are_distinct():
    spec = {"fields": [{"path": "$.items[*].sku", "field": "sku"},
                       {"path": "$.items[*].qty", "field": "qty"}]}
    out = documentstream.convert('{"items":[{"sku":null,"qty":1},{"qty":2},{"sku":"a"}]}', spec)
    records = out["records"]
    assert records[0]["record"] == {"sku": None, "qty": 1} and records[0]["missing"] == []
    assert records[1]["record"] == {"qty": 2} and records[1]["missing"] == ["sku"]
    assert records[2]["record"] == {"sku": "a"} and records[2]["missing"] == ["qty"]
    doc = {"items": [{"sku": None, "qty": 1}, {"qty": 2}, {"sku": "a"}]}
    assert documentpath.select(doc, "$.items[*].sku") == [None, "a"]


def test_scalar_only_mapping_yields_single_root_record():
    spec = {"fields": [{"path": "$.a", "field": "a"}, {"path": "$.b", "field": "b"},
                       {"path": "$.c", "field": "c"}]}
    out = documentstream.convert('{"a":1,"b":null}', spec)
    assert len(out["records"]) == 1
    rec = out["records"][0]
    assert rec["record"] == {"a": 1, "b": None} and rec["missing"] == ["c"]
    assert rec["stream"] == "$" and rec["path"] == "$"
    assert rec["sources"] == {"a": "$.a", "b": "$.b"}


def test_type_mismatch_rejection_is_locatable():
    spec = {"fields": [{"path": "$.items[*].qty", "field": "qty", "type": "integer"}]}
    text = '{"items":[{"qty":3},{"qty":"3"}]}'
    out = documentstream.convert(text, spec)
    assert [r["record"] for r in out["records"]] == [{"qty": 3}]
    rej = out["rejections"][0]
    assert rej["reason"] == "type_mismatch" and rej["field"] == "qty"
    assert rej["expected"] == "integer" and rej["actual"] == "string"
    assert rej["path"] == "$.items[1].qty" and rej["record"] == "$.items[1]"
    assert text[rej["offset"]:].startswith('"3"')


def test_required_field_missing_rejects_record():
    spec = {"fields": [{"path": "$.items[*].sku", "field": "sku", "required": True}]}
    out = documentstream.convert('{"items":[{"sku":"a"},{}]}', spec)
    assert [r["record"] for r in out["records"]] == [{"sku": "a"}]
    assert len(out["rejections"]) == 1
    rej = out["rejections"][0]
    assert rej["reason"] == "missing_required" and rej["field"] == "sku"
    assert rej["path"] == "$.items[1]" and isinstance(rej["offset"], int)


def test_nullable_false_turns_null_into_rejection():
    spec = {"fields": [{"path": "$.items[*].sku", "field": "sku",
                        "type": "string", "nullable": False}]}
    out = documentstream.convert('{"items":[{"sku":"a"},{"sku":null}]}', spec)
    assert [r["record"] for r in out["records"]] == [{"sku": "a"}]
    assert out["rejections"][0]["reason"] == "type_mismatch"
    assert out["rejections"][0]["actual"] == "null"


def test_zip_aligns_sibling_arrays_item_by_item():
    spec = {"arrays": "zip", "fields": [{"path": "$.items[*].sku", "field": "sku"},
                                        {"path": "$.customers[*].name", "field": "name"}]}
    out = documentstream.convert(
        '{"items":[{"sku":"a"},{"sku":"b"}],"customers":[{"name":"x"},{"name":"y"}]}', spec)
    assert [r["record"] for r in out["records"]] == [{"sku": "a", "name": "x"},
                                                     {"sku": "b", "name": "y"}]
    assert out["records"][0]["sources"] == {"sku": "$.items[0].sku",
                                            "name": "$.customers[0].name"}


def test_zip_cardinality_mismatch_rejects_frame():
    spec = {"arrays": "zip", "fields": [{"path": "$.items[*].sku", "field": "sku"},
                                        {"path": "$.customers[*].name", "field": "name"}]}
    out = documentstream.convert('{"items":[{"sku":"a"}],"customers":[{"name":"x"},{"name":"y"}]}',
                                 spec)
    assert out["records"] == []
    rej = out["rejections"][0]
    assert rej["reason"] == "cardinality_mismatch" and rej["path"] == "$"
    assert rej["arrays"] == {"$.items[*]": 1, "$.customers[*]": 2}


def test_zip_nested_frame_per_enclosing_element():
    spec = {"arrays": "zip", "fields": [
        {"path": "$.orders[*].id", "field": "oid"},
        {"path": "$.orders[*].tags[*]", "field": "tag"},
        {"path": "$.orders[*].notes[*]", "field": "note"}]}
    text = '{"orders":[{"id":1,"tags":["a","b"],"notes":["n1","n2"]},' \
           '{"id":2,"tags":["c"],"notes":["n3"]}]}'
    out = documentstream.convert(text, spec)
    got = [(r["path"], r["index"], r["record"]) for r in out["records"]]
    assert got == [("$.orders[0]", 0, {"oid": 1, "tag": "a", "note": "n1"}),
                   ("$.orders[0]", 1, {"oid": 1, "tag": "b", "note": "n2"}),
                   ("$.orders[1]", 0, {"oid": 2, "tag": "c", "note": "n3"})]


def test_zip_rejects_unalignable_frames():
    with pytest.raises(ValueError):
        Conversion({"arrays": "zip", "fields": [{"path": "$.a[*].x[*]", "field": "x"},
                                                {"path": "$.b[*]", "field": "b"}]})


def test_multiple_arrays_require_declared_mode():
    fields = [{"path": "$.a[*]", "field": "a"}, {"path": "$.b[*]", "field": "b"}]
    with pytest.raises(ValueError):
        Conversion({"fields": fields})
    with pytest.raises(ValueError):
        Conversion({"fields": fields, "arrays": "sideways"})


def test_separate_mode_never_forms_cartesian_product():
    fields = [{"path": "$.a[*].v", "field": "v"}, {"path": "$.b[*].w", "field": "w"}]
    text = '{"a":[{"v":1},{"v":2},{"v":3}],"b":[{"w":4},{"w":5}]}'
    out = documentstream.convert(text, {"fields": fields, "arrays": "separate"})
    assert len(out["records"]) == 5  # 3 + 2, never 3 * 2
    assert {r["stream"] for r in out["records"]} == {"$.a[*]", "$.b[*]"}
    zipped = documentstream.convert('{"a":[{"v":1},{"v":2}],"b":[{"w":4},{"w":5}]}',
                                    {"fields": fields, "arrays": "zip"})
    assert len(zipped["records"]) == 2
    assert zipped["rejections"] == []


def test_duplicate_object_key_is_locatable():
    text = '{"a": 1, "a": 2}'
    conv = Conversion({"fields": [{"path": "$.a", "field": "a"}]})
    with pytest.raises(ConversionError) as err:
        conv.feed(text)
    assert err.value.offset == text.index('"a"', 2)
    assert conv.status == "failed"
    conv = Conversion({"fields": [{"path": "$.a", "field": "a"}]})
    assert conv.feed('{"a": 1, "a') == 11
    with pytest.raises(ConversionError):
        conv.feed('": 2}')


def test_truncation_at_tail_revokes_candidates():
    spec = {"fields": [{"path": "$.items[*].sku", "field": "sku"}]}
    conv = Conversion(spec)
    assert conv.feed('{"items":[{"sku":"a"},{"sku":"b"}]') == 34
    assert len(conv.poll()["candidates"]) == 2  # candidates only, nothing confirmed
    with pytest.raises(ConversionError):
        conv.finish()
    assert conv.status == "failed"
    with pytest.raises(ConversionError):
        conv.poll()
    with pytest.raises(ConversionError):
        documentstream.convert('{"items":[{"sku":"a"}', spec)


def test_backpressure_suspends_and_resumes_feed():
    spec = {"fields": [{"path": "$.items[*].v", "field": "v"}],
            "budgets": {"staging": 2}}
    text = '{"items":[{"v":1},{"v":2},{"v":3},{"v":4},{"v":5}]}'
    conv = Conversion(spec)
    got = conv.feed(text)
    assert got < len(text)  # staging filled; consumer must drain
    records = []
    while got < len(text):
        out = conv.poll()
        records += out["candidates"]
        got += conv.feed(text[got:])
    while True:
        out = conv.poll()
        records += out["candidates"]
        if not out["candidates"] and not out["rejections"]:
            break
    assert conv.finish()["confirmed"] == 5
    assert [r["record"] for r in records] == [{"v": n} for n in range(1, 6)]


def test_budgets_bound_parser_and_mapping_state():
    spec = {"fields": [{"path": "$.a", "field": "a"}], "budgets": {"depth": 4}}
    with pytest.raises(ConversionError) as err:
        documentstream.convert('{"a":[[[[[0]]]]]}', spec)
    assert "depth" in err.value.reason
    spec = {"fields": [{"path": "$.a[*]", "field": "a"}], "budgets": {"events": 5}}
    with pytest.raises(ConversionError) as err:
        documentstream.convert('{"a":[1,2,3,4,5,6,7]}', spec)
    assert "event" in err.value.reason
    spec = {"arrays": "zip", "budgets": {"pending": 4},
            "fields": [{"path": "$.a[*]", "field": "a"}, {"path": "$.b[*]", "field": "b"}]}
    with pytest.raises(ConversionError) as err:
        documentstream.convert('{"a":[1,2,3,4,5],"b":[1,2,3,4,5]}', spec)
    assert "pending" in err.value.reason
    spec = {"fields": [{"path": "$.a", "field": "a"}], "budgets": {"token": 4}}
    with pytest.raises(ConversionError) as err:
        documentstream.convert('{"a":"abcdef"}', spec)
    assert "token" in err.value.reason
    with pytest.raises(ValueError):
        Conversion({"fields": [{"path": "$.a", "field": "a"}], "budgets": {"depth": 0}})


def test_scalar_fields_do_not_depend_on_document_order():
    spec = {"fields": [{"path": "$.v", "field": "v"}, {"path": "$.items[*].sku", "field": "sku"}]}
    a = documentstream.convert('{"v":1,"items":[{"sku":"a"}]}', spec)
    b = documentstream.convert('{"items":[{"sku":"a"}],"v":1}', spec)
    assert canon_records(a["records"]) == canon_records(b["records"])
    assert a["records"][0]["record"] == {"v": 1, "sku": "a"}


def test_nested_arrays_denormalize():
    spec = {"fields": [{"path": "$.a[*].k", "field": "k"}, {"path": "$.a[*].b[*].v", "field": "v"}]}
    out = documentstream.convert('{"a":[{"k":1,"b":[{"v":5},{"v":6}]},{"k":2,"b":[{"v":7}]}]}', spec)
    got = [(r["path"], r["record"]) for r in out["records"]]
    assert got == [("$.a[0].b[0]", {"k": 1, "v": 5}), ("$.a[0].b[1]", {"k": 1, "v": 6}),
                   ("$.a[1].b[0]", {"k": 2, "v": 7})]


def test_wildcard_over_object_values():
    spec = {"fields": [{"path": "$.map[*].v", "field": "v"}]}
    out = documentstream.convert('{"map":{"x":{"v":1},"y":{"v":2}}}', spec)
    assert [(r["path"], r["index"], r["record"]) for r in out["records"]] == [
        ("$.map.x", 0, {"v": 1}), ("$.map.y", 1, {"v": 2})]


def test_chunk_boundaries_inside_tokens():
    text = '{"key":"a\\u0041\\n\\uD834\\uDD1E tail","num":-12.5e2,"lit":true}'
    spec = {"fields": [{"path": "$.key", "field": "k"}, {"path": "$.num", "field": "n"},
                       {"path": "$.lit", "field": "b"}]}
    cands, _, summary = run_streaming(text, spec, [1] * len(text))
    assert summary["status"] == "complete"
    assert cands[0]["record"] == {"k": "aA\n𝄞 tail", "n": -1250.0, "b": True}


def test_invalid_documents_fail_with_offsets():
    spec = {"fields": [{"path": "$.a", "field": "a"}]}
    for text in ["", "   ", "{", "[1,]", '{"a":1}x', '"abc', '{"a":01}', "[1e]",
                 '{"a" 1}', '{"a":\x01}', "tru", "[1 2]", "+1", "{,}"]:
        with pytest.raises(ConversionError):
            documentstream.convert(text, spec)
    with pytest.raises(ConversionError):
        documentstream.convert('{"a":1e999}', spec)  # non-finite number


def test_poll_limit_status_and_finish_idempotent():
    spec = {"fields": [{"path": "$.items[*].v", "field": "v"}]}
    conv = Conversion(spec)
    conv.feed('{"items":[{"v":1},{"v":2},{"v":3}]}')
    out = conv.poll(1)
    assert len(out["candidates"]) == 1
    summary = conv.finish()
    assert summary == {"status": "complete", "confirmed": 3, "rejections": 0}
    assert conv.finish() is summary
    assert len(conv.poll()["candidates"]) == 2
    assert conv.status == "complete"
    with pytest.raises(ConversionError):
        conv.feed(" ")
    with pytest.raises(ValueError):
        conv.poll(-1)


def test_api_operation_and_example():
    result = api.OPERATIONS["documentconvert"](api.EXAMPLES["documentconvert"])
    assert result["status"] == "complete" and result["confirmed"] == 2
    assert result["records"][0]["record"] == {"sku": "a", "qty": 2, "batch": "b1"}
    chunked = dict(api.EXAMPLES["documentconvert"])
    doc = chunked.pop("document")
    chunked["chunks"] = [json.dumps(doc)[:10], json.dumps(doc)[10:]]
    again = api.OPERATIONS["documentconvert"](chunked)
    assert canon_records(again["records"]) == canon_records(result["records"])


def test_randomized_crosscheck_against_dom_reference():
    rng = random.Random(20261118)
    tried = 0
    while tried < 250:
        doc = gen_value(rng, 3)
        spec = gen_spec(rng)
        try:
            Conversion(spec)
        except ValueError:
            continue
        tried += 1
        text = json.dumps(doc)
        sizes = [rng.randint(1, 9) for _ in range(20)]
        cands, rejs, summary = run_streaming(text, spec, sizes)
        assert summary["status"] == "complete"
        assert summary["confirmed"] == len(cands)
        assert [c["seq"] for c in cands] == list(range(len(cands)))
        one = documentstream.convert(text, spec)
        assert canon_records(one["records"]) == canon_records(cands)
        assert canon_rejections(one["rejections"]) == canon_rejections(rejs)
        ref_records, ref_rejs = ref_convert(json.loads(text, object_pairs_hook=unique), spec)
        assert canon_records(cands) == canon_records(ref_records)
        assert canon_rejections(rejs) == canon_rejections(ref_rejs)
