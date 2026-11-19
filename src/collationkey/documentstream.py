"""Streaming conversion of chunked JSON documents into analytical records.

Extends the documentpath component from small-document selection to
push-based ingestion of large documents: the source is fed to a Conversion
in text chunks, the request declares a schema mapping of deep field paths
to target record fields, and arrays behind '[*]' steps are expanded into
flat records.

Semantics:

- A field path's scope is its prefix up to and including the last '[*]'
  step. Maximal scopes are the record streams; fields with shallower
  scopes (including wildcard-free scalar fields) join every record below
  them.
- When a mapping spans several sibling arrays the request must declare
  arrays='zip' (elements aligned item by item inside their shared frame,
  cardinality checked per frame) or arrays='separate' (each array expands
  into its own record stream). A Cartesian product is never formed silently.
- Records keep null (a present None value) and missing (absent source)
  apart. Type-incompatible values, missing required fields and array
  cardinality mismatches produce locatable rejection entries; duplicate
  object keys, malformed or truncated input and budget excess fail the
  whole conversion with a locatable ConversionError.
- Records and rejections emitted before finish() succeeds are candidates:
  any document failure revokes them. The parse stack, mapping state,
  capture work and staging queues are all bounded by explicit budgets.
"""
import math
import re
from collections import deque
from . import documentpath


class ConversionError(ValueError):
    """A locatable document or mapping failure; carries the char offset."""

    def __init__(self, reason, offset):
        self.reason = reason
        self.offset = offset
        super().__init__(reason + " at offset " + str(offset))


class _Suspend(Exception):
    """Internal: staging is full; feed() stops and reports consumption."""


_TYPES = {"string", "integer", "number", "boolean", "null", "object", "array", "any"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*$")
_NUM = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?$")
_WS = " \t\n\r"
_HEX = set("0123456789abcdefABCDEF")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_LITERALS = {"t": "true", "f": "false", "n": "null"}
_LITERAL_VALUES = {"true": True, "false": False, "null": None}
_DEFAULT_BUDGETS = {"bytes": 16_000_000, "depth": 64, "events": 2_000_000,
                    "staging": 4096, "pending": 4096, "capture": 262_144, "token": 8192}


def _jtype(value):
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


def _type_ok(value, ftype, nullable):
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
    return False  # ftype == "null" rejects non-null values


def _render_step(step, out):
    kind, arg = step
    if kind == "field":
        if _IDENT.match(arg):
            out.append("." + arg)
        else:
            out.append('["' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"]')
    elif kind == "index":
        out.append("[" + str(arg) + "]")
    else:
        out.append("[*]")


def _render(steps):
    out = ["$"]
    for step in steps:
        _render_step(step, out)
    return "".join(out)


def _prefix(a, b):
    return len(a) <= len(b) and b[:len(a)] == a


def _common(a, b):
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    return k


class _Field:
    __slots__ = ("fid", "path", "steps", "name", "type", "required", "nullable",
                 "scope", "rest", "scope_id")


def _analyze(spec):
    """Validate a conversion spec; return the compiled mapping plan."""
    if not isinstance(spec, dict):
        raise ValueError("spec object required")
    raw = spec.get("fields")
    if not isinstance(raw, list) or not raw:
        raise ValueError("spec requires a nonempty fields list")
    if len(raw) > 128:
        raise ValueError("too many field mappings")
    fields, names, paths = [], set(), set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("field mapping must be an object")
        path = item.get("path")
        name = item.get("field")
        ftype = item.get("type", "any")
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ValueError("target field name invalid")
        if name in names:
            raise ValueError("duplicate target field " + name)
        if not isinstance(path, str):
            raise ValueError("field path must be a string")
        if path in paths:
            raise ValueError("duplicate field path " + path)
        steps = tuple(documentpath.parse(path))
        if ftype not in _TYPES:
            raise ValueError("unknown field type " + str(ftype))
        last = -1
        for k, step in enumerate(steps):
            if step[0] == "all":
                last = k
        f = _Field()
        f.fid, f.path, f.steps, f.name = i, path, steps, name
        f.type = ftype
        f.required = bool(item.get("required", False))
        f.nullable = bool(item.get("nullable", True))
        f.scope = steps[:last + 1]
        f.rest = steps[last + 1:]
        fields.append(f)
        names.add(name)
        paths.add(path)
    scopes = sorted({f.scope for f in fields})
    scope_ids = {s: k for k, s in enumerate(scopes)}
    for f in fields:
        f.scope_id = scope_ids[f.scope]
    leaves = [s for s in scopes if not any(o != s and _prefix(s, o) for o in scopes)]
    arrays = spec.get("arrays")
    if arrays not in (None, "zip", "separate"):
        raise ValueError("arrays must be 'zip' or 'separate'")
    if len(leaves) > 1 and arrays is None:
        raise ValueError("mapping spans multiple arrays; declare arrays: 'zip' or 'separate'")
    mode = arrays or "separate"
    frame = None
    if mode == "zip" and len(leaves) > 1:
        locs = [leaf[:-1] for leaf in leaves]
        frame = locs[0]
        for loc in locs[1:]:
            frame = frame[:_common(frame, loc)]
        for loc in locs:
            if any(step[0] == "all" for step in loc[len(frame):]):
                raise ValueError("arrays cannot zip: " + _render(loc) +
                                 " nests further arrays outside the shared frame")
    budgets = dict(_DEFAULT_BUDGETS)
    given = spec.get("budgets", {})
    if not isinstance(given, dict):
        raise ValueError("budgets must be an object")
    for key, value in given.items():
        if (key not in budgets or not isinstance(value, int) or isinstance(value, bool)
                or not 1 <= value <= 2**31):
            raise ValueError("invalid budget " + str(key))
        budgets[key] = value
    return fields, scopes, scope_ids, leaves, mode, frame, budgets


class Conversion:
    """Chunked JSON to record conversion with candidate/confirmed output.

    Protocol: feed() text chunks (it returns the number of characters
    consumed; a short count means staging is full, so drain with poll() and
    re-feed the remainder), poll() drains candidate records and rejections,
    and finish() validates the document tail and confirms every candidate.
    A ConversionError from feed()/finish() fails the conversion and revokes
    all candidates emitted so far; poll() then raises as well.
    """

    def __init__(self, spec):
        fields, scopes, scope_ids, leaves, mode, frame, budgets = _analyze(spec)
        self.fields = fields
        self.scopes = scopes
        self.scope_ids = scope_ids
        self.leaves = leaves
        self.zip = mode == "zip" and len(leaves) > 1
        self.frame = frame
        self.budgets = budgets
        self.leaf_sids = [scope_ids[leaf] for leaf in leaves]
        self.leaf_sid_set = set(self.leaf_sids)
        self.frame_id = len(scopes)
        self.scope_names = {scope_ids[s]: _render(s) for s in scopes}
        self.at_scope = {}
        for f in fields:
            self.at_scope.setdefault(f.scope_id, []).append(f.fid)
        # Structural trie over field paths, scope paths and the zip frame.
        self.nodes = [{"edges": {}, "field": None, "scope": None, "frame": False}]
        for f in fields:
            self._trie_add(f.steps, field=f.fid)
        for s in scopes:
            self._trie_add(s, scope=scope_ids[s])
        if self.zip:
            self._trie_add(frame, frame=True)
        # Record shapes: stream key -> field ids in spec order.
        self.streams = {}
        self.stream_names = {}
        if self.zip:
            self.streams[self.frame_id] = [f.fid for f in fields]
            self.stream_names[self.frame_id] = _render(frame)
        else:
            for leaf in leaves:
                sid = scope_ids[leaf]
                self.streams[sid] = [f.fid for f in fields if _prefix(f.scope, leaf)]
                self.stream_names[sid] = _render(leaf)
        # Parser state.
        self.status = "active"
        self.error = None
        self.summary = None
        self.offset = 0
        self.events = 0
        self.pstack = []
        self.pmode = "value"
        self.first = False
        self.pendkey = None
        self.tok = []
        self.skey = False
        self.sstate = "norm"
        self.high = None
        self.uhex = ""
        self.lit = ""
        self.lpos = 0
        self.voff = 0
        self.koff = 0
        # Mapper state.
        self.mstack = [frozenset({0})]
        self.cstack = ["$"]
        self.istack = []
        self.mirrors = []
        self.capture_nodes = 0
        self.pending_records = 0
        self.ready = deque()
        self.candidates = deque()
        self.rejections = deque()
        self.stalled = False
        self.total_candidates = 0
        self.total_rejections = 0

    # ------------------------------------------------------------------ trie
    def _trie_add(self, steps, field=None, scope=None, frame=False):
        nid = 0
        for step in steps:
            edges = self.nodes[nid]["edges"]
            if step not in edges:
                edges[step] = len(self.nodes)
                self.nodes.append({"edges": {}, "field": None, "scope": None, "frame": False})
            nid = edges[step]
        node = self.nodes[nid]
        if field is not None:
            node["field"] = field
        if scope is not None:
            node["scope"] = scope
        if frame:
            node["frame"] = True

    def _advance(self, ms, step):
        out = set()
        kind, arg = step
        for nid in ms:
            edges = self.nodes[nid]["edges"]
            for key in ((kind, arg), ("all", None)):
                nxt = edges.get(key)
                if nxt is not None:
                    out.add(nxt)
        return frozenset(out)

    # ---------------------------------------------------------------- parser
    def feed(self, chunk):
        """Consume a text chunk; returns characters consumed.

        A return value shorter than len(chunk) signals backpressure: drain
        staged output with poll() and re-feed the unconsumed remainder.
        """
        if self.status != "active":
            raise ConversionError("conversion already " + self.status, self.offset)
        if not isinstance(chunk, str):
            raise ValueError("chunk must be a string")
        if self.stalled:
            self._flush(False)
            if self.stalled:
                return 0
        i, n = 0, len(chunk)
        try:
            while i < n:
                if self.offset + i >= self.budgets["bytes"]:
                    raise ConversionError("byte budget exceeded", self.offset + i)
                self._step(chunk[i], self.offset + i)
                i += 1
                if self.ready:
                    self._flush(True)
        except _Suspend:
            self.offset += i
            return i
        except ConversionError as e:
            self.offset += i
            self.status = "failed"
            self.error = e
            raise
        self.offset += n
        if self.ready:
            self._flush(False)
        return n

    def finish(self):
        """Declare end of input; confirms all candidates on success."""
        if self.status == "failed":
            raise self.error
        if self.status == "complete":
            return self.summary
        try:
            if self.pmode == "number":
                self._number_done(self.offset)
            if self.pmode != "done":
                raise ConversionError("truncated document", self.offset)
            while self.istack:
                self._close_instance(self.istack.pop())
            self._flush(False)
        except ConversionError as e:
            self.status = "failed"
            self.error = e
            raise
        self.status = "complete"
        self.summary = {"status": "complete", "confirmed": self.total_candidates,
                        "rejections": self.total_rejections}
        return self.summary

    def poll(self, limit=None):
        """Drain staged candidate records and rejections (up to limit each)."""
        if self.status == "failed":
            raise ConversionError("conversion failed; candidate records are revoked", self.offset)
        if limit is None:
            limit = 2**31
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("invalid poll limit")
        cands = [self.candidates.popleft() for _ in range(min(limit, len(self.candidates)))]
        rejs = [self.rejections.popleft() for _ in range(min(limit, len(self.rejections)))]
        self._flush(False)
        return {"candidates": cands, "rejections": rejs}

    def _step(self, c, off):
        mode = self.pmode
        if mode == "value":
            self._step_value(c, off)
        elif mode == "key":
            self._step_key(c, off)
        elif mode == "colon":
            if c in _WS:
                return
            if c != ":":
                raise ConversionError("expected ':' after object key", off)
            self.pmode = "value"
            self.first = False
        elif mode == "comma":
            self._step_comma(c, off)
        elif mode == "string":
            self._step_string(c, off)
        elif mode == "number":
            self._step_number(c, off)
        elif mode == "literal":
            self._step_literal(c, off)
        else:  # done
            if c not in _WS:
                raise ConversionError("trailing data after document", off)

    def _step_value(self, c, off):
        if c in _WS:
            return
        if c == "]":
            if self.pstack and self.pstack[-1]["kind"] == "array" and self.first:
                self._close_container("array", off)
                return
            raise ConversionError("unexpected ]", off)
        self._begin_value(off)
        if c == "{":
            self._push("object", off)
        elif c == "[":
            self._push("array", off)
        elif c == '"':
            self.pmode = "string"
            self.skey = False
            self.tok = []
            self.sstate = "norm"
            self.high = None
        elif c in "-0123456789":
            self.pmode = "number"
            self.tok = [c]
        elif c in "tfn":
            self.pmode = "literal"
            self.lit = _LITERALS[c]
            self.lpos = 1
        else:
            raise ConversionError("unexpected character " + repr(c), off)

    def _step_key(self, c, off):
        if c in _WS:
            return
        if c == "}" and self.first:
            self._close_container("object", off)
            return
        if c != '"':
            raise ConversionError("object key expected", off)
        self.pmode = "string"
        self.skey = True
        self.tok = []
        self.sstate = "norm"
        self.high = None
        self.koff = off

    def _step_comma(self, c, off):
        if c in _WS:
            return
        top = self.pstack[-1]
        if c == ",":
            self.pmode = "value" if top["kind"] == "array" else "key"
            self.first = False
        elif c == "]":
            if top["kind"] != "array":
                raise ConversionError("mismatched ]", off)
            self._close_container("array", off)
        elif c == "}":
            if top["kind"] != "object":
                raise ConversionError("mismatched }", off)
            self._close_container("object", off)
        else:
            raise ConversionError("expected ',' or closing bracket", off)

    def _step_string(self, c, off):
        state = self.sstate
        if state == "u":
            if c not in _HEX:
                raise ConversionError("invalid unicode escape", off)
            self.uhex += c
            if len(self.uhex) == 4:
                unit = int(self.uhex, 16)
                self.uhex = ""
                self.sstate = "norm"
                self._string_unit(unit, off)
            return
        if state == "esc":
            if self.high is not None and c != "u":
                raise ConversionError("unpaired surrogate", off)
            if c == "u":
                self.sstate = "u"
                self.uhex = ""
            elif c in _ESCAPES:
                self._tok_append(_ESCAPES[c], off)
                self.sstate = "norm"
            else:
                raise ConversionError("invalid escape", off)
            return
        if c == '"':
            if self.high is not None:
                raise ConversionError("unpaired surrogate", off)
            self._string_done(off)
        elif c == "\\":
            self.sstate = "esc"
        elif ord(c) < 0x20:
            raise ConversionError("control character in string", off)
        else:
            if self.high is not None:
                raise ConversionError("unpaired surrogate", off)
            self._tok_append(c, off)

    def _string_unit(self, unit, off):
        if self.high is not None:
            if not 0xDC00 <= unit <= 0xDFFF:
                raise ConversionError("unpaired surrogate", off)
            self._tok_append(chr(0x10000 + ((self.high - 0xD800) << 10) + unit - 0xDC00), off)
            self.high = None
        elif 0xD800 <= unit <= 0xDBFF:
            self.high = unit
        elif 0xDC00 <= unit <= 0xDFFF:
            raise ConversionError("unpaired surrogate", off)
        else:
            self._tok_append(chr(unit), off)

    def _tok_append(self, c, off):
        self.tok.append(c)
        if len(self.tok) > self.budgets["token"]:
            raise ConversionError("token budget exceeded", off)

    def _string_done(self, off):
        text = "".join(self.tok)
        self.tok = []
        if self.skey:
            top = self.pstack[-1]
            if text in top["keys"]:
                raise ConversionError("duplicate object key " + repr(text), self.koff)
            top["keys"].add(text)
            self.pendkey = text
            self._key(text)
            self.pmode = "colon"
        else:
            self._scalar(text, self.voff)
            self._value_done(off)

    def _step_number(self, c, off):
        if c in "-+0123456789.eE":
            self._tok_append(c, off)
        else:
            self._number_done(off)
            self._step(c, off)

    def _number_done(self, off):
        text = "".join(self.tok)
        self.tok = []
        if not _NUM.match(text):
            raise ConversionError("invalid number " + repr(text), self.voff)
        value = float(text) if any(ch in text for ch in ".eE") else int(text)
        if isinstance(value, float) and not math.isfinite(value):
            raise ConversionError("number out of range", self.voff)
        self._scalar(value, self.voff)
        self._value_done(off)

    def _step_literal(self, c, off):
        if c != self.lit[self.lpos]:
            raise ConversionError("invalid literal", off)
        self.lpos += 1
        if self.lpos == len(self.lit):
            self._scalar(_LITERAL_VALUES[self.lit], self.voff)
            self._value_done(off)

    def _begin_value(self, off):
        if self.pstack:
            top = self.pstack[-1]
            step = ("index", top["count"]) if top["kind"] == "array" else ("field", self.pendkey)
        else:
            step = None
        self.events += 1
        if self.events > self.budgets["events"]:
            raise ConversionError("event budget exceeded", off)
        self.voff = off
        self.first = False
        self._begin(step, off)

    def _push(self, kind, off):
        if len(self.pstack) + 1 > self.budgets["depth"]:
            raise ConversionError("depth budget exceeded", off)
        self.pstack.append({"kind": kind, "keys": set(), "count": 0})
        self._open(kind, off)
        self.pmode = "key" if kind == "object" else "value"
        self.first = True

    def _close_container(self, kind, off):
        self.pstack.pop()
        self._close(kind, off)
        self._value_done(off)

    def _value_done(self, off):
        if self.pstack:
            self.pstack[-1]["count"] += 1
            self.pmode = "comma"
        else:
            self.pmode = "done"

    # ---------------------------------------------------------------- mapper
    def _begin(self, step, off):
        ms = self.mstack[-1] if step is None else self._advance(self.mstack[-1], step)
        self._cur_ms = ms
        conc = self.cstack[-1]
        if step is not None:
            parts = [conc]
            _render_step(step, parts)
            conc = "".join(parts)
        self._cur_concrete = conc
        self._cur_captures = []
        for nid in ms:
            node = self.nodes[nid]
            if node["scope"] is not None or node["frame"]:
                self._open_instance(node, conc, off)
        for nid in ms:
            node = self.nodes[nid]
            if node["field"] is not None:
                f = self.fields[node["field"]]
                target = self._find_instance(f.scope_id)
                if target is None:
                    raise ConversionError("mapping state error", off)
                self._cur_captures.append((f.fid, target, conc, off))

    def _open(self, kind, off):
        self.mstack.append(self._cur_ms)
        self.cstack.append(self._cur_concrete)
        for m in list(self.mirrors):
            self._mirror_open(m, kind, off)
        for fid, target, conc, voff in self._cur_captures:
            self._capture_tick(off)
            self.mirrors.append({"fid": fid, "target": target, "conc": conc, "off": voff,
                                 "stack": [[] if kind == "array" else {}], "key": None})
        self._cur_captures = []

    def _close(self, kind, off):
        for m in list(self.mirrors):
            self._mirror_close(m, kind, off)
        self.mstack.pop()
        self.cstack.pop()
        self._close_level(len(self.pstack))

    def _scalar(self, value, off):
        for m in list(self.mirrors):
            self._mirror_value(m, value, off)
        for fid, target, conc, voff in self._cur_captures:
            self._capture_tick(off)
            self._bind(target, fid, value, conc, voff)
        self._cur_captures = []
        self._close_level(len(self.pstack))

    def _key(self, text):
        for m in self.mirrors:
            m["key"] = text

    def _open_instance(self, node, conc, off):
        sid = node["scope"]
        inst = {"path_id": sid if sid is not None else self.frame_id,
                "frame": node["frame"], "concrete": conc, "offset": off,
                "ordinal": self.pstack[-1]["count"] if self.pstack else 0,
                "level": len(self.pstack), "captures": {},
                "pending": set(self.at_scope.get(sid, ())) if sid is not None else set(),
                "waiting": [],
                "slots": {leaf: [] for leaf in self.leaf_sids} if node["frame"] else None}
        self.istack.append(inst)

    def _find_instance(self, scope_id):
        for inst in reversed(self.istack):
            if inst["path_id"] == scope_id:
                return inst
        return None

    def _find_frame(self):
        for inst in reversed(self.istack):
            if inst["frame"]:
                return inst
        return None

    def _close_level(self, level):
        while self.istack and self.istack[-1]["level"] == level:
            self._close_instance(self.istack.pop())

    def _close_instance(self, inst):
        for fid in list(inst["pending"]):
            inst["pending"].discard(fid)
            self._notify(inst, fid, ("missing",))
        sid = inst["path_id"]
        if sid in self.leaf_sid_set:
            if self.zip:
                frame = self._find_frame()
                if frame is None:
                    raise ConversionError("mapping state error", inst["offset"])
                frame["slots"][sid].append(inst)
            else:
                self._make_record(inst)
        if inst["frame"]:
            self._make_zip_records(inst)

    def _bind(self, inst, fid, value, conc, off):
        provider = ("value", value, conc, off)
        inst["captures"][fid] = provider
        inst["pending"].discard(fid)
        self._notify(inst, fid, provider)

    def _notify(self, inst, fid, provider):
        keep = []
        for rec, rfid in inst["waiting"]:
            if rfid == fid:
                rec["providers"][fid] = provider
                rec["count"] -= 1
                if rec["count"] == 0:
                    self._release(rec)
            else:
                keep.append((rec, rfid))
        inst["waiting"] = keep

    def _make_record(self, inst):
        sid = inst["path_id"]
        rec = {"stream": self.stream_names[sid], "path": inst["concrete"],
               "index": inst["ordinal"], "offset": inst["offset"],
               "shape": self.streams[sid], "providers": {}, "count": 0}
        for fid in rec["shape"]:
            f = self.fields[fid]
            if f.scope_id == sid:
                rec["providers"][fid] = inst["captures"].get(fid, ("missing",))
                continue
            target = self._find_instance(f.scope_id)
            if target is not None and fid in target["captures"]:
                rec["providers"][fid] = target["captures"][fid]
            elif target is not None and fid in target["pending"]:
                rec["count"] += 1
                target["waiting"].append((rec, fid))
            else:
                rec["providers"][fid] = ("missing",)
        self._park(rec)

    def _make_zip_records(self, frame_inst):
        counts = {leaf: len(frame_inst["slots"][leaf]) for leaf in self.leaf_sids}
        if len(set(counts.values())) > 1:
            self.total_rejections += 1
            self._enqueue_ready(("rejection", {
                "reason": "cardinality_mismatch", "path": frame_inst["concrete"],
                "arrays": {self.scope_names[leaf]: counts[leaf] for leaf in self.leaf_sids},
                "offset": frame_inst["offset"]}))
            return
        for i in range(counts[self.leaf_sids[0]]):
            rec = {"stream": self.stream_names[self.frame_id], "path": frame_inst["concrete"],
                   "index": i, "offset": frame_inst["offset"],
                   "shape": self.streams[self.frame_id], "providers": {},
                   "count": 0}
            for fid in rec["shape"]:
                f = self.fields[fid]
                if f.scope_id in self.leaf_sid_set:
                    slot = frame_inst["slots"][f.scope_id][i]
                    rec["providers"][fid] = slot["captures"].get(fid, ("missing",))
                elif f.scope_id == frame_inst["path_id"]:
                    rec["providers"][fid] = frame_inst["captures"].get(fid, ("missing",))
                else:
                    target = self._find_instance(f.scope_id)
                    if target is not None and fid in target["captures"]:
                        rec["providers"][fid] = target["captures"][fid]
                    elif target is not None and fid in target["pending"]:
                        rec["count"] += 1
                        target["waiting"].append((rec, fid))
                    else:
                        rec["providers"][fid] = ("missing",)
            self._park(rec)

    def _park(self, rec):
        self.pending_records += 1
        if self.pending_records > self.budgets["pending"]:
            raise ConversionError("pending record budget exceeded", rec["offset"])
        if rec["count"] == 0:
            self._release(rec)

    def _release(self, rec):
        self.pending_records -= 1  # leaves the parked set; output re-counts below
        out, missing, sources, bad = {}, [], {}, []
        for fid in rec["shape"]:
            f = self.fields[fid]
            p = rec["providers"][fid]
            if p[0] == "missing":
                if f.required:
                    bad.append({"reason": "missing_required", "field": f.name,
                                "path": rec["path"], "offset": rec["offset"]})
                else:
                    missing.append(f.name)
            else:
                _, value, src, off = p
                if _type_ok(value, f.type, f.nullable):
                    out[f.name] = value
                    sources[f.name] = src
                else:
                    bad.append({"reason": "type_mismatch", "field": f.name,
                                "expected": f.type, "actual": _jtype(value),
                                "path": src, "record": rec["path"], "offset": off})
        if bad:
            for entry in bad:
                self.total_rejections += 1
                self._enqueue_ready(("rejection", entry))
        else:
            cand = {"seq": self.total_candidates, "stream": rec["stream"],
                    "path": rec["path"], "index": rec["index"], "offset": rec["offset"],
                    "record": out, "missing": missing, "sources": sources}
            self.total_candidates += 1
            self._enqueue_ready(("candidate", cand))

    def _enqueue_ready(self, item):
        self.pending_records += 1
        if self.pending_records > self.budgets["pending"]:
            raise ConversionError("pending record budget exceeded", self.offset)
        self.ready.append(item)

    # -------------------------------------------------------------- captures
    def _capture_tick(self, off):
        self.capture_nodes += 1
        if self.capture_nodes > self.budgets["capture"]:
            raise ConversionError("capture budget exceeded", off)

    def _mirror_open(self, m, kind, off):
        container = [] if kind == "array" else {}
        if m["stack"]:
            self._mirror_attach(m, container, off)
        m["stack"].append(container)

    def _mirror_close(self, m, kind, off):
        value = m["stack"].pop()
        if not m["stack"]:
            self.mirrors.remove(m)
            self._bind(m["target"], m["fid"], value, m["conc"], m["off"])

    def _mirror_value(self, m, value, off):
        if m["stack"]:
            self._mirror_attach(m, value, off)
        else:
            self.mirrors.remove(m)
            self._bind(m["target"], m["fid"], value, m["conc"], m["off"])

    def _mirror_attach(self, m, value, off):
        top = m["stack"][-1]
        if isinstance(top, list):
            top.append(value)
        else:
            top[m["key"]] = value
            m["key"] = None
        self._capture_tick(off)

    # --------------------------------------------------------------- staging
    def _flush(self, strict):
        while self.ready and len(self.candidates) + len(self.rejections) < self.budgets["staging"]:
            kind, item = self.ready.popleft()
            (self.candidates if kind == "candidate" else self.rejections).append(item)
            self.pending_records -= 1
        self.stalled = bool(self.ready)
        if strict and self.stalled:
            raise _Suspend()


def convert(chunks, spec):
    """One-shot conversion: feed all chunks, finish, return confirmed output.

    chunks may be a single string or an iterable of strings. The result has
    status, confirmed record count, records and rejections.
    """
    if isinstance(chunks, str):
        chunks = [chunks]
    conv = Conversion(spec)
    records, rejections = [], []

    def drain():
        out = conv.poll()
        records.extend(out["candidates"])
        rejections.extend(out["rejections"])

    for chunk in chunks:
        if not isinstance(chunk, str):
            raise ValueError("chunks must be strings")
        pos = 0
        while pos < len(chunk):
            pos += conv.feed(chunk[pos:])
            drain()
    drain()
    summary = conv.finish()
    drain()
    return {"status": summary["status"], "confirmed": summary["confirmed"],
            "records": records, "rejections": rejections}
