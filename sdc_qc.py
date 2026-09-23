#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sdc_qc.py - Static SDC quality checker against a gate-level Verilog netlist
               and CCS/LVF Liberty models (pre-APR QC).

What it is:   a structural checker. It executes each mode's SDC in a real Tcl
              interpreter (tkinter.Tcl) with SDC/PrimeTime-style commands
              implemented against the parsed netlist + Liberty, records every
              constraint, and runs rule checks per mode and across modes.
What it isn't: an STA engine. No delay calculation, no slack. PrimeTime /
              Tempus check_timing remains the signoff authority.

Python: 3.6.2 baseline, tested on 3.9.7. Standard library only (+ tkinter).

Run `python3 sdc_qc.py -h` for options, or see run.sh.
"""

from __future__ import print_function

import argparse
import array
import bisect
import collections
import concurrent.futures
import gzip
import hashlib
import json
import math
import os
import pickle
import re
import shutil
import subprocess
import sys
import time

VERSION = "1.0.0"
LIB_EXTRACT_VERSION = 1          # bump when the Liberty extract format changes

SEV_ERROR, SEV_WARNING, SEV_INFO = "ERROR", "WARNING", "INFO"
_SEV_RANK = {SEV_ERROR: 0, SEV_WARNING: 1, SEV_INFO: 2}


def _fatal(msg):
    """Tool/usage error: exit status 2 (1 is reserved for 'ERROR findings')."""
    sys.stderr.write(msg + "\n")
    sys.exit(2)


def _log(msg):
    sys.stderr.write("[sdc_qc %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


class Finding(object):
    __slots__ = ("rule", "sev", "mode", "file", "line", "cmd", "obj", "msg")

    def __init__(self, rule, sev, mode, file, line, cmd, obj, msg):
        self.rule, self.sev, self.mode = rule, sev, mode
        self.file, self.line, self.cmd, self.obj, self.msg = file, line, cmd, obj, msg

    def as_dict(self):
        return dict((k, getattr(self, k)) for k in self.__slots__)


# Rule catalogue: id -> (default severity, short title). Printed in the report.
RULES = collections.OrderedDict([
    ("PARSE-001", (SEV_ERROR, "Tcl error while executing SDC")),
    ("PARSE-002", (SEV_ERROR, "Unknown command")),
    ("PARSE-003", (SEV_ERROR, "Unknown or malformed command option")),
    ("PARSE-004", (SEV_ERROR, "Sourced / mode SDC file not found")),
    ("PARSE-005", (SEV_INFO, "Tool command accepted but not modelled")),
    ("PARSE-006", (SEV_WARNING, "Unsupported attribute in get_attribute / -filter")),
    ("PARSE-007", (SEV_WARNING, "Deferred feature used (result approximated)")),
    ("DES-001", (SEV_ERROR, "current_design does not match netlist top")),
    ("DES-002", (SEV_ERROR, "Unresolved cell reference (no module, no Liberty cell)")),
    ("DES-003", (SEV_INFO, "Reference black-boxed by -blackbox")),
    ("DES-004", (SEV_WARNING, "Netlist parse issue")),
    ("DES-005", (SEV_WARNING, "Instance pin not found on Liberty cell / module")),
    ("LIB-001", (SEV_ERROR, "Liberty read/parse problem")),
    ("LIB-002", (SEV_WARNING, "Cell defined in several libraries with different pins")),
    ("UNIT-001", (SEV_ERROR, "set_units does not match Liberty units")),
    ("UNIT-002", (SEV_WARNING, "Liberty files use different units")),
    ("UNIT-003", (SEV_WARNING, "Clock periods look implausible for the time unit")),
    ("OBJ-001", (SEV_ERROR, "Object query matched nothing")),
    ("OBJ-002", (SEV_ERROR, "Constraint dropped: object option resolved to nothing")),
    ("OBJ-003", (SEV_ERROR, "Object of wrong type for option")),
    ("CLK-001", (SEV_WARNING, "Clock redefined (same name)")),
    ("CLK-002", (SEV_WARNING, "Clock source overwritten (another clock, no -add)")),
    ("CLK-003", (SEV_ERROR, "Invalid clock period")),
    ("CLK-004", (SEV_ERROR, "Invalid clock waveform")),
    ("CLK-005", (SEV_WARNING, "Questionable clock source object")),
    ("CLK-006", (SEV_ERROR, "Generated clock -source missing or unresolved")),
    ("CLK-007", (SEV_ERROR, "Generated clock -master_clock undefined")),
    ("CLK-008", (SEV_WARNING, "Generated clock master ambiguous / not found at -source")),
    ("CLK-009", (SEV_ERROR, "Invalid generated clock definition")),
    ("CLK-010", (SEV_WARNING, "Virtual clock never referenced")),
    ("CLK-011", (SEV_ERROR, "No clocks defined in mode")),
    ("CLK-012", (SEV_INFO, "set_propagated_clock in a pre-CTS SDC")),
    ("COV-001", (SEV_WARNING, "Register/macro clock pins not reached by any clock")),
    ("COV-002", (SEV_INFO, "Clock trace stopped at logic that is not buffer/inverter/ICG")),
    ("COV-003", (SEV_WARNING, "Register/macro clock pin tied constant or undriven")),
    ("IO-001", (SEV_WARNING, "Input port without set_input_delay")),
    ("IO-002", (SEV_WARNING, "Output port without set_output_delay")),
    ("IO-003", (SEV_ERROR, "I/O constraint on port of wrong direction")),
    ("IO-004", (SEV_ERROR, "I/O delay without a valid -clock")),
    ("IO-005", (SEV_WARNING, "I/O delay large relative to clock period")),
    ("IO-006", (SEV_WARNING, "I/O delay has -max but no -min (or vice versa)")),
    ("IO-007", (SEV_WARNING, "Input port without driving cell / drive / input transition")),
    ("IO-008", (SEV_WARNING, "Output port without set_load")),
    ("IO-009", (SEV_INFO, "I/O delay on internal pin (not modelled)")),
    ("EXC-001", (SEV_WARNING, "-from object is not a valid timing startpoint")),
    ("EXC-002", (SEV_WARNING, "-to object is not a valid timing endpoint")),
    ("EXC-003", (SEV_WARNING, "Setup multicycle without matching hold multicycle")),
    ("EXC-004", (SEV_WARNING, "Duplicate / overlapping timing exception")),
    ("EXC-005", (SEV_INFO, "Exception redundant with set_clock_groups")),
    ("EXC-006", (SEV_WARNING, "Exception without -from/-to/-through (applies to all paths)")),
    ("CG-001", (SEV_ERROR, "Clock in more than one group of a set_clock_groups")),
    ("CG-002", (SEV_INFO, "set_clock_groups with a single -group")),
    ("CASE-001", (SEV_ERROR, "Invalid set_case_analysis value")),
    ("CASE-002", (SEV_WARNING, "Conflicting set_case_analysis on the same object")),
    ("MISC-001", (SEV_ERROR, "Invalid pin name in set_disable_timing -from/-to")),
    ("MISC-002", (SEV_WARNING, "Unsupported set_hierarchy_separator")),
    ("MODE-001", (SEV_WARNING, "Clock defined in some modes only")),
    ("MODE-002", (SEV_WARNING, "Same clock name differs across modes")),
    ("MODE-003", (SEV_WARNING, "Port I/O-delay coverage differs across modes")),
    ("MODE-004", (SEV_WARNING, "Port drive/load coverage differs across modes")),
])


# =============================================================================
# 1. Liberty extraction
#
# CCS/LVF libraries are several GB; >95% is table data (CCS current vectors,
# LVF sigma tables, delay/power tables). The scanner only interprets the groups
# we need (library/cell/pin/bus/timing/type/ff/latch) and fast-forwards over
# every other group by brace counting with bytes.find/count (C speed), never
# tokenizing table contents. Extracts are cached on disk (pickle) keyed by
# path + size + mtime, so repeated QC runs load them in milliseconds.
# Several libraries are parsed in parallel *processes* (the GIL makes threads
# useless for CPU-bound parsing).
# =============================================================================

_LIB_EVENT = re.compile(rb"[{}]|/\*")
_LIB_ATTR = re.compile(rb'([A-Za-z_][\w.]*)\s*:\s*("(?:[^"\\]|\\.)*"|[^;{}"]*?)\s*;')
_LIB_CATTR = re.compile(rb"([A-Za-z_]\w*)\s*\(([^(){};]*)\)\s*;")
_LIB_HDR = re.compile(rb"([A-Za-z_][\w.]*)\s*\(([^(){}]*)\)\s*$")
_LIB_COMMENT = re.compile(rb"/\*.*?\*/", re.S)
_LIB_BITSEL = re.compile(r"^(.*?)\[(\d+)(?::(\d+))?\]$")

# Parser contexts
_C_ROOT, _C_LIB, _C_CELL, _C_PIN, _C_BUS, _C_TIMING, _C_TYPE, _C_FF, _C_LATCH = range(9)
_CHILD = {
    _C_ROOT: {b"library": _C_LIB},
    _C_LIB: {b"cell": _C_CELL, b"type": _C_TYPE},
    _C_CELL: {b"pin": _C_PIN, b"bus": _C_BUS, b"ff": _C_FF, b"ff_bank": _C_FF,
              b"latch": _C_LATCH, b"latch_bank": _C_LATCH, b"type": _C_TYPE},
    _C_BUS: {b"pin": _C_PIN, b"timing": _C_TIMING},
    _C_PIN: {b"timing": _C_TIMING},
}
# Groups inside a cell that only set a flag (content skipped)
_CELL_FLAG_GROUPS = {b"statetable": "seq_statetable", b"memory": "memory"}
_ATTR_CONTEXTS = (_C_LIB, _C_CELL, _C_PIN, _C_BUS, _C_TIMING, _C_TYPE, _C_FF, _C_LATCH)

_READ_CHUNK = 64 * 1024 * 1024


def _unq(b):
    s = b.decode("latin-1").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.strip()


def _open_lib_stream(path):
    """Return (fileobj, proc). .gz is decompressed by pigz/gzip in a separate
    process when available (overlaps decompression with parsing)."""
    if path.endswith(".gz"):
        exe = shutil.which("pigz") or shutil.which("gzip")
        if exe:
            proc = subprocess.Popen([exe, "-dc", path], stdout=subprocess.PIPE,
                                    bufsize=_READ_CHUNK)
            return proc.stdout, proc
        return gzip.open(path, "rb"), None
    return open(path, "rb"), None


class _LibParse(object):
    """Incremental state machine; see module comment above."""

    def __init__(self):
        self.lib = {"name": None, "time_unit": None, "cap_unit": None,
                    "cells": {}, "types": {}, "errors": []}
        self.stack = [(_C_ROOT, None)]
        self.cell = None

    # ---- attribute handling -------------------------------------------------
    def attrs(self, ctx, obj, seg):
        if b"/*" in seg:
            seg = _LIB_COMMENT.sub(b" ", seg)
        if ctx == _C_LIB:
            for m in _LIB_ATTR.finditer(seg):
                if m.group(1) == b"time_unit":
                    self.lib["time_unit"] = _unq(m.group(2))
            for m in _LIB_CATTR.finditer(seg):
                if m.group(1) == b"capacitive_load_unit":
                    self.lib["cap_unit"] = _unq(m.group(2)).replace(" ", "")
            return
        for m in _LIB_ATTR.finditer(seg):
            k, v = m.group(1), m.group(2)
            if ctx == _C_TIMING:
                if k == b"related_pin":
                    obj["rp"] = _unq(v)
                elif k == b"timing_type":
                    obj["tt"] = _unq(v)
            elif ctx in (_C_PIN, _C_BUS):
                if k in (b"direction", b"clock", b"function", b"bus_type",
                         b"clock_gate_clock_pin", b"clock_gate_enable_pin",
                         b"clock_gate_out_pin"):
                    obj["attrs"][k.decode()] = _unq(v)
                    if ctx == _C_BUS and k == b"bus_type":
                        self._bus_bits(obj)
            elif ctx == _C_CELL:
                if k == b"clock_gating_integrated_cell":
                    self.cell["icg"] = _unq(v)
                elif k == b"is_macro_cell":
                    self.cell["macro"] = _unq(v).lower() == "true"
            elif ctx == _C_TYPE:
                obj[k.decode()] = _unq(v)
            elif ctx == _C_FF:
                if k in (b"clocked_on", b"clocked_on_also"):
                    self.cell["clk_expr"].append(_unq(v))
            elif ctx == _C_LATCH:
                if k in (b"enable", b"enable_also"):
                    self.cell["clk_expr"].append(_unq(v))

    def _type_range(self, tname):
        t = (self.cell or {}).get("types", {}).get(tname) or self.lib["types"].get(tname)
        if not t:
            return None
        try:
            return int(t.get("bit_from", 0)), int(t.get("bit_to", 0))
        except ValueError:
            return None

    def _bus_bits(self, bus):
        if bus["bits"] is not None:
            return
        rng = self._type_range(bus["attrs"].get("bus_type"))
        if rng is None:
            self.lib["errors"].append("cell %s bus %s: unknown bus_type %s"
                                      % (self.cell["name"], bus["name"],
                                         bus["attrs"].get("bus_type")))
            bus["bits"] = []
            return
        a, b = rng
        step = 1 if b >= a else -1
        bus["bits"] = ["%s[%d]" % (bus["name"], i) for i in range(a, b + step, step)]
        self.cell["bus"][bus["name"]] = list(bus["bits"])
        for bit in bus["bits"]:
            self._pin(bit)

    def _pin(self, name):
        pins = self.cell["pins"]
        rec = pins.get(name)
        if rec is None:
            rec = pins[name] = {"dir": None, "clock": False, "function": None, "arcs": [],
                                "cg": None}
        return rec

    # ---- group open / close -------------------------------------------------
    def open_group(self, seg):
        """Called on '{' in an interpreted context. Returns True to interpret
        the new group, False to skip it."""
        ctx, obj = self.stack[-1]
        kids = _CHILD.get(ctx)
        if not kids:
            return False
        m = _LIB_HDR.search(seg[-1024:])
        if not m:
            return False
        gname, garg = m.group(1), m.group(2)
        if ctx == _C_CELL and gname in _CELL_FLAG_GROUPS:
            flag = _CELL_FLAG_GROUPS[gname]
            if flag == "memory":
                self.cell["memory"] = True
            else:
                self.cell["seq"] = self.cell["seq"] or "statetable"
            return False
        nctx = kids.get(gname)
        if nctx is None:
            return False
        arg = _unq(garg)
        if nctx == _C_LIB:
            self.lib["name"] = arg
            nobj = None
        elif nctx == _C_CELL:
            self.cell = {"name": arg, "pins": {}, "bus": {}, "seq": None, "clk_expr": [],
                         "icg": None, "macro": False, "memory": False, "types": {}}
            nobj = self.cell
        elif nctx == _C_TYPE:
            nobj = {}
            (self.cell["types"] if ctx == _C_CELL else self.lib["types"])[arg] = nobj
        elif nctx == _C_BUS:
            nobj = {"name": arg, "attrs": {}, "bits": None, "arcs": []}
        elif nctx == _C_PIN:
            names = []
            for part in arg.split(","):
                part = part.strip().strip('"')
                if not part:
                    continue
                bm = _LIB_BITSEL.match(part)
                if bm and bm.group(3) is not None:
                    a, b = int(bm.group(2)), int(bm.group(3))
                    st = 1 if b >= a else -1
                    names.extend("%s[%d]" % (bm.group(1), i) for i in range(a, b + st, st))
                else:
                    names.append(part)
            if ctx == _C_BUS:
                self._bus_bits(obj)
            nobj = {"names": names, "attrs": {}, "arcs": []}
        elif nctx == _C_TIMING:
            nobj = {"rp": None, "tt": "combinational"}
        elif nctx == _C_FF:
            self.cell["seq"] = "ff"
            nobj = None
        elif nctx == _C_LATCH:
            if self.cell["seq"] != "ff":
                self.cell["seq"] = "latch"
            nobj = None
        else:
            nobj = None
        self.stack.append((nctx, nobj))
        return True

    def close_group(self):
        ctx, obj = self.stack.pop()
        if not self.stack:
            self.lib["errors"].append("unbalanced '}'")
            self.stack = [(_C_ROOT, None)]
            return
        pctx, pobj = self.stack[-1]
        if ctx == _C_TIMING:
            arc = (obj["rp"] or "", obj["tt"])
            pobj["arcs"].append(arc)
        elif ctx == _C_PIN:
            inherited = pobj["attrs"] if pctx == _C_BUS else {}
            for n in obj["names"]:
                rec = self._pin(n)
                self._apply(rec, inherited)
                self._apply(rec, obj["attrs"])
                rec["arcs"].extend(obj["arcs"])
        elif ctx == _C_BUS:
            self._bus_bits(obj)
            for bit in obj["bits"]:
                rec = self._pin(bit)
                if rec["dir"] is None:
                    self._apply(rec, obj["attrs"])
                rec["arcs"].extend(obj["arcs"])
        elif ctx == _C_CELL:
            c = obj
            del c["types"]
            self.lib["cells"][c.pop("name")] = c
            self.cell = None

    @staticmethod
    def _apply(rec, attrs):
        if "direction" in attrs:
            rec["dir"] = attrs["direction"]
        if attrs.get("clock", "").lower() == "true":
            rec["clock"] = True
        if "function" in attrs:
            rec["function"] = attrs["function"]
        for k in ("clock_gate_clock_pin", "clock_gate_enable_pin", "clock_gate_out_pin"):
            if attrs.get(k, "").lower() == "true":
                rec["cg"] = k[len("clock_gate_"):-len("_pin")]


def parse_liberty(path):
    """Worker entry point: parse one .lib/.lib.gz, return the compact extract."""
    t0 = time.time()
    st = _LibParse()
    fobj, proc = _open_lib_stream(path)
    buf = b""
    pos = 0          # scan position in buf
    last = 0         # start of the current (unprocessed) segment
    skip = 0         # depth inside a skipped group
    eof = False
    nbytes = 0
    try:
        while True:
            if not eof:
                chunk = fobj.read(_READ_CHUNK)
                if chunk:
                    nbytes += len(chunk)
                    keep = pos if skip else last
                    buf = buf[keep:] + chunk
                    pos -= keep
                    last -= keep if not skip else 0
                    if skip:
                        last = 0
                else:
                    eof = True
            need_more = False
            while True:
                if skip:
                    # fast-forward: one iteration per '}' in the skipped region
                    j = buf.find(b"}", pos)
                    if j < 0:
                        skip += buf.count(b"{", pos)
                        pos = len(buf)
                        need_more = True
                        break
                    skip += buf.count(b"{", pos, j) - 1
                    pos = j + 1
                    if skip == 0:
                        last = pos
                    continue
                m = _LIB_EVENT.search(buf, pos)
                if m is None:
                    need_more = True
                    break
                i = m.start()
                ch = buf[i]
                if ch == 47:  # '/*' comment: jump over it
                    j = buf.find(b"*/", i + 2)
                    if j < 0:
                        need_more = True
                        break
                    pos = j + 2
                    continue
                ctx, obj = st.stack[-1]
                seg = buf[last:i]
                if ctx in _ATTR_CONTEXTS and seg.strip():
                    st.attrs(ctx, obj, seg)
                if ch == 123:  # '{'
                    if not st.open_group(seg):
                        skip = 1
                        pos = i + 1
                        continue
                else:
                    st.close_group()
                pos = last = i + 1
            if need_more and eof:
                break
            if not need_more:
                continue
        if skip or len(st.stack) > 1:
            st.lib["errors"].append("unexpected end of file (unbalanced braces; "
                                    "possibly a brace inside a comment or string)")
    finally:
        fobj.close()
        if proc is not None:
            proc.wait()
            if proc.returncode:
                st.lib["errors"].append("decompressor exited with code %d" % proc.returncode)
    lib = st.lib
    del lib["types"]
    lib["path"] = path
    lib["bytes"] = nbytes
    lib["seconds"] = round(time.time() - t0, 2)
    if lib["name"] is None:
        lib["errors"].append("no library() group found")
    return lib


def _lib_cache_file(cache_dir, path):
    h = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:20]
    return os.path.join(cache_dir, "%s_%s.pkl" % (os.path.basename(path), h))


def _lib_stamp(path):
    s = os.stat(path)
    return (os.path.abspath(path), s.st_size, int(s.st_mtime), LIB_EXTRACT_VERSION)


def load_liberties(paths, jobs, cache_dir):
    """Returns list of extracts (same order as paths). Uses cache, then parses
    the rest in parallel processes."""
    results = [None] * len(paths)
    todo = []
    for i, p in enumerate(paths):
        if not os.path.isfile(p):
            results[i] = {"path": p, "name": None, "cells": {}, "time_unit": None,
                          "cap_unit": None, "errors": ["file not found"], "bytes": 0,
                          "seconds": 0, "cached": False}
            continue
        if cache_dir:
            cf = _lib_cache_file(cache_dir, p)
            try:
                with open(cf, "rb") as f:
                    stamp, lib = pickle.load(f)
                if stamp == _lib_stamp(p):
                    lib["cached"] = True
                    results[i] = lib
                    continue
            except Exception:
                pass
        todo.append(i)
    if todo:
        _log("parsing %d Liberty file(s) with %d process(es)" % (len(todo), max(1, min(jobs, len(todo)))))
        if jobs > 1 and len(todo) > 1:
            with concurrent.futures.ProcessPoolExecutor(max_workers=min(jobs, len(todo))) as ex:
                futs = dict((ex.submit(parse_liberty, paths[i]), i) for i in todo)
                for fu in concurrent.futures.as_completed(futs):
                    i = futs[fu]
                    try:
                        results[i] = fu.result()
                    except Exception as e:  # pragma: no cover
                        results[i] = {"path": paths[i], "name": None, "cells": {},
                                      "time_unit": None, "cap_unit": None,
                                      "errors": ["parser crashed: %r" % (e,)],
                                      "bytes": 0, "seconds": 0}
        else:
            for i in todo:
                results[i] = parse_liberty(paths[i])
        for i in todo:
            results[i]["cached"] = False
            if cache_dir and not results[i]["errors"]:
                try:
                    if not os.path.isdir(cache_dir):
                        os.makedirs(cache_dir)
                    tmp = _lib_cache_file(cache_dir, paths[i]) + ".%d.tmp" % os.getpid()
                    with open(tmp, "wb") as f:
                        pickle.dump((_lib_stamp(paths[i]), results[i]), f, protocol=4)
                    os.rename(tmp, _lib_cache_file(cache_dir, paths[i]))
                except Exception as e:
                    _log("warning: cannot write Liberty cache: %s" % e)
    return results


# =============================================================================
# 2. Reference (cell type) model built from Liberty extracts / Verilog modules
# =============================================================================

_CONSTRAINT_TT = ("setup_", "hold_", "recovery_", "removal_", "nochange_", "non_seq_",
                  "skew_")
_EDGE_TT = ("rising_edge", "falling_edge")
_RX_IDENT = re.compile(r"[A-Za-z_][\w\[\]]*")

REF_LIB, REF_MODULE, REF_BLACKBOX, REF_UNRESOLVED = "lib", "module", "blackbox", "unresolved"
_DIR_MAP = {"input": "in", "output": "out", "inout": "inout", "internal": "internal",
            "in": "in", "out": "out"}


class Ref(object):
    """A cell type: Liberty cell, Verilog module, or unknown (black box)."""
    __slots__ = ("name", "kind", "pins", "pidx", "pdir", "bus", "seq", "clock_pins",
                 "endpoint_pins", "out_pins", "in_pins", "buf_in", "inv", "icg_in",
                 "macro", "memory", "lib", "unknown_pins")

    def __init__(self, name, kind):
        self.name, self.kind = name, kind
        self.pins, self.pidx, self.pdir, self.bus = [], {}, [], {}
        self.seq = None            # "ff" | "latch" | "statetable" | "arcs" | None
        self.clock_pins = frozenset()
        self.endpoint_pins = frozenset()
        self.out_pins, self.in_pins = [], []
        self.buf_in = -1           # buffer/inverter: index of the single input
        self.inv = False
        self.icg_in = -1           # ICG: index of the clock input
        self.macro = self.memory = False
        self.lib = None
        self.unknown_pins = set()

    def add_pin(self, name, direction=None):
        i = self.pidx.get(name)
        if i is None:
            i = self.pidx[name] = len(self.pins)
            self.pins.append(name)
            self.pdir.append(direction)
            bm = _LIB_BITSEL.match(name)
            if bm and bm.group(3) is None:
                self.bus.setdefault(bm.group(1), []).append(i)
        return i

    def finalize_dirs(self):
        self.out_pins = [i for i, d in enumerate(self.pdir) if d in ("out", "inout")]
        self.in_pins = [i for i, d in enumerate(self.pdir) if d in ("in", "inout")]


def _strip_fn(f):
    return f.replace(" ", "").replace("(", "").replace(")", "") if f else ""


def ref_from_lib(name, cell, libname):
    r = Ref(name, REF_LIB)
    r.lib = libname
    for pname, p in cell["pins"].items():
        r.add_pin(pname, _DIR_MAP.get((p["dir"] or "").lower()))
    r.finalize_dirs()
    clock, endpoint = set(), set()
    has_arc_seq = False
    for pname, p in cell["pins"].items():
        i = r.pidx[pname]
        if p["clock"]:
            clock.add(i)
        for rp, tt in p["arcs"]:
            if tt.startswith(_CONSTRAINT_TT):
                endpoint.add(i)
                has_arc_seq = True
                for q in rp.split():
                    if q in r.pidx:
                        clock.add(r.pidx[q])
            elif tt in _EDGE_TT:
                has_arc_seq = True
                for q in rp.split():
                    if q in r.pidx:
                        clock.add(r.pidx[q])
    for expr in cell["clk_expr"]:
        for tok in _RX_IDENT.findall(expr):
            if tok in r.pidx:
                clock.add(r.pidx[tok])
    r.seq = cell["seq"] or ("arcs" if has_arc_seq else None)
    r.macro, r.memory = cell.get("macro", False), cell.get("memory", False)
    if cell.get("icg"):
        cin = [r.pidx[n] for n, p in cell["pins"].items() if p["cg"] == "clock"]
        if not cin:
            cin = [i for i in clock if r.pdir[i] == "in"]
        if len(cin) == 1 and len(r.out_pins) == 1:
            r.icg_in = cin[0]
        clock.update(cin)
    elif not r.seq and len(r.in_pins) == 1 and len(r.out_pins) == 1:
        ip = r.pins[r.in_pins[0]]
        fn = _strip_fn(cell["pins"][r.pins[r.out_pins[0]]]["function"])
        if fn == ip:
            r.buf_in = r.in_pins[0]
        elif fn in ("!" + ip, ip + "'"):
            r.buf_in, r.inv = r.in_pins[0], True
    r.clock_pins = frozenset(clock)
    r.endpoint_pins = frozenset(endpoint)
    return r


def _pin_sig(cell):
    return tuple(sorted((n, p["dir"]) for n, p in cell["pins"].items()))


# =============================================================================
# 3. Gate-level Verilog netlist (Design Compiler style) -> flat design database
#
# Memory layout is sized for 1-2M instances: per-cell data lives in array()s,
# cell pin connections are one flat array of net ids (slot = cell_off[c] + pin
# index), nets connected across hierarchy / assign share one id (union-find).
# =============================================================================

CONST0, CONST1, UNCONN = -2, -3, -1
_ID = r"(?:\\\S+|[A-Za-z_][\w$]*)"
_RX_V_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_RX_MOD_HDR = re.compile(r"(?<![\w$\\])module\s+(" + _ID + r")\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?(?:\((.*?)\))?\s*;", re.S)
_RX_STMT = re.compile(r"[^;]*;")
_RX_DECL = re.compile(r"^\s*(input|output|inout|wire|tri|wand|wor|supply0|supply1|assign|reg)\b(.*)$", re.S)
_RX_RANGE = re.compile(r"^\s*(?:signed\s+)?\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\](.*)$", re.S)
_RX_INST = re.compile(r"\s*(" + _ID + r")\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?(" + _ID +
                      r")\s*(\[[^\]]*\])?\s*\((.*)\)\s*;\s*$", re.S)
_RX_NAMED = re.compile(r"\.\s*(\\\S+|[\w$]+)\s*\(((?:[^()]|\([^()]*\))*)\)")
_RX_CONST = re.compile(r"^(\d*)\s*'\s*[sS]?([bBhHdDoO])\s*([0-9a-fA-FxXzZ_?]+)$")
_RX_SEL = re.compile(r"^(\\\S+|[A-Za-z_][\w$]*)\s*(?:\[\s*(-?\d+)\s*(?::\s*(-?\d+)\s*)?\])?$")


def _vname(tok):
    tok = tok.strip()
    if tok.startswith("\\"):
        return tok[1:].rstrip()
    return tok


def _split_top(s, sep=","):
    if "{" not in s and "(" not in s:
        return s.split(sep)
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class Module(object):
    __slots__ = ("name", "file", "ports", "pdecl", "wires", "assigns", "text", "body",
                 "issues")

    def __init__(self, name, file):
        self.name, self.file = name, file
        self.ports = []           # port names in header order
        self.pdecl = {}           # name -> (dir, msb, lsb) ; msb=None for scalar
        self.wires = []           # [(range or None, [names])] per declaration
        self.assigns = []         # (lhs, rhs) strings
        self.text = None          # whole (comment-stripped) file text, shared
        self.body = (0, 0)        # [start, end) of the module body in text
        self.issues = []

    def port_bits(self):
        out = []
        for p in self.ports:
            d = self.pdecl.get(p)
            if d is None:
                out.append((p, None))
                continue
            dr, msb, lsb = d
            if msb is None:
                out.append((p, dr))
            else:
                st = -1 if msb > lsb else 1
                out.extend(("%s[%d]" % (p, i), dr) for i in range(msb, lsb + st, st))
        return out


_DECL_KWS = ("input", "output", "inout", "wire", "tri", "wand", "wor", "supply0", "supply1",
             "assign", "reg")
_WS = " \t\r\n"


def _decl_spans(text, bs, be):
    """Declaration statements in a module body, found with str.find (C speed)
    instead of classifying every one of the ~millions of statements in Python."""
    spans = []
    for kw in _DECL_KWS:
        i = text.find(kw, bs, be)
        while i >= 0:
            j = i - 1
            while j >= bs and text[j] in _WS:
                j -= 1
            nxt = text[i + len(kw):i + len(kw) + 1]
            if (j < bs or text[j] == ";") and (nxt in _WS or nxt == "["):
                e = text.find(";", i, be)
                if e < 0:
                    break
                spans.append((i, e + 1))
                i = text.find(kw, e + 1, be)
            else:
                i = text.find(kw, i + len(kw), be)
    spans.sort()
    return spans


def read_verilog_modules(path):
    """Pass 1: find modules, their ports and declarations. Instance statements
    are parsed only once, during elaboration (in parallel for big modules)."""
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            text = f.read().decode("latin-1")
    else:
        with open(path, "rb") as f:
            text = f.read().decode("latin-1")
    if "//" in text or "/*" in text:
        text = _RX_V_COMMENT.sub(" ", text)
    mods = []
    pos = 0
    while True:
        m = _RX_MOD_HDR.search(text, pos)
        if not m:
            break
        mod = Module(_vname(m.group(1)), path)
        hdr = m.group(2) or ""
        body_start = m.end()
        e = body_start
        while True:
            e = text.find("endmodule", e)
            if e < 0:
                break
            before = text[e - 1] if e else " "
            after = text[e + 9:e + 10]
            if not (before.isalnum() or before in "_$\\") and not (after.isalnum() or after in "_$"):
                break
            e += 9
        if e < 0:
            mod.issues.append("module %s: missing endmodule" % mod.name)
            e = len(text)
        mod.text = text
        mod.body = (body_start, e)
        if re.search(r"\b(input|output|inout)\b", hdr):       # ANSI header
            for part in _split_top(hdr):
                _decl_ports(mod, part.strip(), header=True)
        else:
            mod.ports = [_vname(p) for p in _split_top(hdr) if p.strip()]
        for a, b in _decl_spans(text, body_start, e):
            st = text[a:b]
            dm = _RX_DECL.match(st)
            if dm is None:
                continue
            kw, rest = dm.group(1), dm.group(2)[:-1]
            if kw in ("input", "output", "inout"):
                _decl_ports(mod, st[:-1].strip(), header=False)
            elif kw == "assign":
                for a2 in _split_top(rest):
                    if "=" in a2:
                        lhs, rhs = a2.split("=", 1)
                        mod.assigns.append((lhs.strip(), rhs.strip()))
            else:
                rm = _RX_RANGE.match(rest)
                rng = None
                if rm:
                    rng = (int(rm.group(1)), int(rm.group(2)))
                    rest = rm.group(3)
                if "\\" not in rest and "=" not in rest:
                    names = [n.strip() for n in rest.split(",")]
                else:
                    names = [_vname(n.split("=")[0]) for n in _split_top(rest)]
                mod.wires.append((rng, [n for n in names if n]))
        mods.append(mod)
        pos = e + 9
    return mods


def _decl_ports(mod, s, header):
    m = re.match(r"^\s*(input|output|inout)\s+(?:wire\s+|reg\s+)?(.*)$", s, re.S)
    if not m:
        if header and s:
            mod.ports.append(_vname(s))
        return
    dr = {"input": "in", "output": "out", "inout": "inout"}[m.group(1)]
    rest = m.group(2)
    rm = _RX_RANGE.match(rest)
    msb = lsb = None
    if rm:
        msb, lsb, rest = int(rm.group(1)), int(rm.group(2)), rm.group(3)
    for n in _split_top(rest):
        n = _vname(n)
        if not n:
            continue
        mod.pdecl[n] = (dr, msb, lsb)
        if header:
            mod.ports.append(n)


class Port(object):
    __slots__ = ("name", "dir", "net")

    def __init__(self, name, d, net):
        self.name, self.dir, self.net = name, d, net


class Design(object):
    """Flattened design database."""

    def __init__(self):
        self.top = None
        self.refs, self.ref_index = [], {}
        self.cell_names, self.cell_index = [], {}
        self.cell_ref = array.array("i")
        self.cell_off = array.array("q", [0])
        self.conn = array.array("i")
        self.net_names, self.net_index = [], {}
        self.net_bus = collections.defaultdict(list)
        self.net_parent = None           # union-find (only when assigns exist)
        self.ports, self.port_index = [], {}
        self.port_bus = collections.defaultdict(list)
        self.issues = []                 # (rule, msg)
        self.driver = None               # net -> slot (>=0) or -(2+port) or -1
        self._csr = None
        self.libs = []
        self.lib_cells = {}              # (lib, cell) -> Ref (all lib cells, lazily)
        self.time_unit = None
        self.cap_unit = None
        self.const_nets = {}             # net -> CONST0/CONST1 (from assign / tie)
        self.lib_refs = {}

    # ---- nets -----------------------------------------------------------------
    def new_net(self, name):
        i = len(self.net_names)
        self.net_names.append(name)
        self.net_index[name] = i
        return i

    def find(self, n):
        p = self.net_parent
        if p is None or n < 0 or n >= len(p):   # nets created after the last union
            return n
        root = n
        while p[root] != root:
            root = p[root]
        while p[n] != root:
            p[n], n = root, p[n]
        return root

    def union(self, a, b):
        if self.net_parent is None:
            self.net_parent = array.array("i", range(len(self.net_names)))
        p = self.net_parent
        while len(p) < len(self.net_names):
            p.append(len(p))
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            p[rb] = ra

    # ---- queries --------------------------------------------------------------
    def cell_slot(self, c, pidx):
        return self.cell_off[c] + pidx

    def slot_net(self, slot):
        n = self.conn[slot]
        return self.find(n) if n >= 0 else n

    def slot_cell(self, slot):
        return bisect.bisect_right(self.cell_off, slot) - 1

    def ncell_pins(self, c):
        return self.cell_off[c + 1] - self.cell_off[c]

    def pin_name(self, c, p):
        return self.cell_names[c] + "/" + self.refs[self.cell_ref[c]].pins[p]

    def pin_dir(self, c, p):
        return self.refs[self.cell_ref[c]].pdir[p]

    def build_driver_map(self):
        n = len(self.net_names)
        drv = array.array("q", [-1]) * n
        conn, off, cref, refs = self.conn, self.cell_off, self.cell_ref, self.refs
        find = self.find
        outs_of = [r.out_pins if r.kind != REF_MODULE else [] for r in refs]
        for c in range(len(self.cell_names)):
            outs = outs_of[cref[c]]
            if outs:
                base = off[c]
                lim = off[c + 1] - base
                for k in outs:
                    if k < lim:
                        net = conn[base + k]
                        if net >= 0:
                            drv[find(net)] = base + k
        for pi, p in enumerate(self.ports):
            if p.dir in ("in", "inout") and p.net >= 0:
                drv[find(p.net)] = -(2 + pi)
        self.driver = drv

    def net_pins(self, net):
        """All (cell, pin) on a net (lazy CSR index; built on first use)."""
        if self._csr is None:
            t0 = time.time()
            n = len(self.net_names)
            cnt = array.array("q", [0]) * (n + 1)
            conn, find = self.conn, self.find
            for v in conn:
                if v >= 0:
                    cnt[find(v) + 1] += 1
            for i in range(n):
                cnt[i + 1] += cnt[i]
            fill = array.array("q", cnt)
            slots = array.array("q", [0]) * len(conn)
            for s, v in enumerate(conn):
                if v >= 0:
                    r = find(v)
                    slots[fill[r]] = s
                    fill[r] += 1
            self._csr = (cnt, slots)
            _log("built net->pin index in %.1fs" % (time.time() - t0))
        cnt, slots = self._csr
        net = self.find(net)
        out = []
        for k in range(cnt[net], cnt[net + 1]):
            s = slots[k]
            c = self.slot_cell(s)
            out.append((c, s - self.cell_off[c]))
        return out


_HAS_FORK = hasattr(os, "fork")    # no fork (Windows): elaborate serially
_PAR_ELAB_BYTES = 16 * 1024 * 1024     # module bodies larger than this parse in parallel


def _split_ranges(text, bs, be, n):
    """Split [bs, be) into ~n ranges ending on ';' statement boundaries."""
    if n <= 1:
        return [(bs, be)]
    out, a = [], bs
    step = (be - bs) // n
    for k in range(1, n):
        t = text.find(";", bs + k * step, be)
        if t < 0 or t + 1 <= a:
            continue
        out.append((a, t + 1))
        a = t + 1
    out.append((a, be))
    return out


def _elab_chunk(rng):
    """Parse the instance statements in text[rng]. Runs in a forked worker (or
    inline); reads its context from _G['elab'] (inherited through fork)."""
    text, prefix, local, refinfo, buses = _G["elab"]
    start, end = rng
    names, reflist, refid = [], [], {}
    rref = array.array("i")
    conn = array.array("i")
    rowlen = array.array("i")
    slow = array.array("q")
    patch = []
    get_local = local.get
    rx_inst, rx_named, rx_decl = _RX_INST, _RX_NAMED, _RX_DECL
    for sm in _RX_STMT.finditer(text, start, end):
        s = sm.group(0)
        m = rx_inst.match(s)
        if m is None:
            if not rx_decl.match(s):
                slow.append(sm.start())
                slow.append(sm.end())
            continue
        refname = m.group(1)
        info = refinfo.get(refname)
        body = m.group(4)
        if info is None or m.group(3) or not body.lstrip().startswith("."):
            slow.append(sm.start())
            slow.append(sm.end())
            continue
        npins, pidx = info
        row = [UNCONN] * npins
        pend = None
        ok = True
        for pin, expr in rx_named.findall(body):
            i = pidx.get(pin)
            if i is None:
                ok = False
                break
            expr = expr.strip()
            if not expr:
                continue
            n = get_local(expr)
            if n is None:
                if expr[0] == "\\":
                    n = get_local(expr[1:].rstrip())
                elif expr == "1'b0":
                    n = CONST0
                elif expr == "1'b1":
                    n = CONST1
                if n is None:
                    nm = expr[1:].rstrip() if expr[0] == "\\" else expr
                    if nm in buses or " " in nm or "{" in nm or ":" in nm or "'" in nm:
                        ok = False
                        break
                    if pend is None:
                        pend = []
                    pend.append((i, nm))
                    continue
            row[i] = n
        if not ok:
            slow.append(sm.start())
            slow.append(sm.end())
            continue
        iname = m.group(2)
        if iname[0] == "\\":
            iname = iname[1:].rstrip()
        names.append(prefix + iname)
        ri = refid.get(refname)
        if ri is None:
            ri = refid[refname] = len(reflist)
            reflist.append(refname)
        rref.append(ri)
        if pend:
            b0 = len(conn)
            patch.extend((b0 + i, nm) for i, nm in pend)
        conn.extend(row)
        rowlen.append(npins)
    return names, rref, reflist, conn, rowlen, slow, patch


class Elaborator(object):
    def __init__(self, design, modules, lib_refs, blackbox_patterns, jobs=1):
        self.d = design
        self.jobs = max(1, jobs)
        self.modules = modules
        self.lib_refs = lib_refs          # cell name -> Ref (from Liberty)
        self.bb = [re.compile(_glob_to_re(p, hier=False, simple=True)) for p in blackbox_patterns]
        self.unknown_pin_warned = set()
        self.stats = collections.Counter()

    def ref_for(self, name):
        d = self.d
        ri = d.ref_index.get(name)
        if ri is not None:
            return ri
        if name in self.modules:
            r = Ref(name, REF_MODULE)
            for pn, pdir in self.modules[name].port_bits():
                r.add_pin(pn, pdir)
            r.finalize_dirs()
        elif name in self.lib_refs:
            r = self.lib_refs[name]
        elif any(rx.match(name) for rx in self.bb):
            r = Ref(name, REF_BLACKBOX)
            d.issues.append(("DES-003", "reference %s black-boxed" % name))
        else:
            r = Ref(name, REF_UNRESOLVED)
            d.issues.append(("DES-002", "reference %s is neither a netlist module nor a Liberty "
                                        "cell (provide its netlist or .lib, or use -blackbox)" % name))
        ri = d.ref_index[name] = len(d.refs)
        d.refs.append(r)
        return ri

    def elaborate(self, top):
        d = self.d
        mod = self.modules[top]
        d.top = top
        local = d.net_index      # top-level local names == global net names
        buses = {}
        for pn, pdir in mod.port_bits():
            net = d.new_net(pn)
            pi = len(d.ports)
            d.ports.append(Port(pn, pdir, net))
            d.port_index[pn] = pi
            bm = _LIB_BITSEL.match(pn)
            if bm and bm.group(3) is None:
                d.port_bus[bm.group(1)].append(pi)
            if pdir is None:
                d.issues.append(("DES-004", "top port %s has no direction declaration" % pn))
        self._elab_module(mod, "", local, buses)

    def _declare(self, mod, prefix, local, buses):
        d = self.d
        for p, (dr, msb, lsb) in mod.pdecl.items():
            if msb is not None:
                st = -1 if msb > lsb else 1
                buses[p] = ["%s[%d]" % (p, i) for i in range(msb, lsb + st, st)]
        net_names, net_index = d.net_names, d.net_index
        shared = local is net_index
        for rng, wnames in mod.wires:
            for w in wnames:
                if rng is None:
                    names = (w,)
                else:
                    msb, lsb = rng
                    st = -1 if msb > lsb else 1
                    names = ["%s[%d]" % (w, i) for i in range(msb, lsb + st, st)]
                    buses[w] = names
                for n in names:
                    if n not in local:
                        i = len(net_names)
                        full = prefix + n
                        net_names.append(full)
                        net_index[full] = i
                        if not shared:
                            local[n] = i
                if rng is not None:
                    d.net_bus[prefix + w].extend(local[n] for n in names)

    def _bits(self, expr, prefix, local, buses):
        """Verilog expression -> list of net ids (MSB first)."""
        e = expr.strip()
        if not e:
            return []
        if e[0] == "{":
            inner = e[1:-1].strip()
            rm = re.match(r"^(\d+)\s*\{(.*)\}$", inner, re.S)
            if rm:
                return self._bits("{" + rm.group(2) + "}", prefix, local, buses) * int(rm.group(1))
            out = []
            for part in _split_top(inner):
                out.extend(self._bits(part, prefix, local, buses))
            return out
        if "'" in e:
            cm = _RX_CONST.match(e.replace(" ", ""))
            if cm:
                width = int(cm.group(1) or 32)
                base = {"b": 2, "h": 16, "d": 10, "o": 8}[cm.group(2).lower()]
                digits = cm.group(3).replace("_", "")
                try:
                    val = int(digits, base)
                except ValueError:
                    val = 0
                return [CONST1 if (val >> i) & 1 else CONST0 for i in range(width - 1, -1, -1)]
        if e.startswith("\\"):
            sp = e.find(" ")
            if sp < 0:
                name, sel = e[1:], ""
            else:
                name, sel = e[1:sp], e[sp:].strip()
        else:
            b = e.find("[")
            name, sel = (e, "") if b < 0 else (e[:b].strip(), e[b:].replace(" ", ""))
        if not sel:
            if name in buses:
                return [self._net(n, prefix, local) for n in buses[name]]
            return [self._net(name, prefix, local)]
        sm = re.match(r"^\[(-?\d+)(?::(-?\d+))?\]$", sel)
        if not sm:
            self.d.issues.append(("DES-004", "cannot parse expression '%s'" % e))
            return []
        if sm.group(2) is None:
            return [self._net("%s[%s]" % (name, sm.group(1)), prefix, local)]
        a, b = int(sm.group(1)), int(sm.group(2))
        st = -1 if a > b else 1
        return [self._net("%s[%d]" % (name, i), prefix, local) for i in range(a, b + st, st)]

    def _net(self, name, prefix, local):
        n = local.get(name)
        if n is None:
            n = local[name] = self.d.new_net(prefix + name)
        return n

    def _elab_module(self, mod, prefix, local, buses):
        d = self.d
        self._declare(mod, prefix, local, buses)
        for lhs, rhs in mod.assigns:
            lb = self._bits(lhs, prefix, local, buses)
            rb = self._bits(rhs, prefix, local, buses)
            for a, b in zip(lb[::-1], rb[::-1]):
                if a >= 0 and b >= 0:
                    d.union(a, b)
                elif a >= 0:
                    d.const_nets[a] = b
        text = mod.text
        bs, be = mod.body
        children = []
        # Fast path (possibly in parallel worker processes): plain Liberty-cell
        # instances with named single-bit connections to known nets. Everything
        # else (hier instances, buses, unknown pins/nets) is returned as "slow"
        # statement spans and handled serially below.
        refinfo = dict((n, (len(r.pins), r.pidx)) for n, r in self.lib_refs.items())
        _G["elab"] = (text, prefix, local, refinfo, buses)
        jobs = self.jobs if (be - bs) > _PAR_ELAB_BYTES and _HAS_FORK else 1
        ranges = _split_ranges(text, bs, be, jobs * 4 if jobs > 1 else 1)
        if jobs > 1:
            import multiprocessing
            with multiprocessing.get_context("fork").Pool(jobs) as pool:
                parts = pool.map(_elab_chunk, ranges, chunksize=1)
        else:
            parts = [_elab_chunk(r) for r in ranges]
        _G.pop("elab", None)
        cell_off = d.cell_off
        for names, rref, reflist, conn, rowlen, slow, patch in parts:
            base = len(d.conn)
            rid = [self.ref_for(n) for n in reflist]
            c0 = len(d.cell_names)
            d.cell_names.extend(names)
            d.cell_index.update(zip(names, range(c0, c0 + len(names))))
            d.cell_ref.extend(array.array("i", [rid[x] for x in rref]))
            off = cell_off[-1]
            for ln in rowlen:
                off += ln
                cell_off.append(off)
            d.conn.extend(conn)
            for pos, nm in patch:             # implicit (undeclared) nets
                d.conn[base + pos] = self._net(nm, prefix, local)
            for k in range(0, len(slow), 2):
                self._inst_stmt(mod, text[slow[k]:slow[k + 1]], prefix, local, buses, children)
        del parts
        # recurse into hierarchical instances
        refs, new_net = d.refs, d.new_net
        for c, ri in children:
            r = refs[ri]
            child = self.modules[r.name]
            base = cell_off[c]
            clocal = {}
            for i, pn in enumerate(r.pins):
                n = d.conn[base + i]
                if n >= 0:
                    clocal[pn] = n
                    d.net_index.setdefault(d.cell_names[c] + "/" + pn, n)
                elif n in (CONST0, CONST1):
                    nn = new_net(d.cell_names[c] + "/" + pn)
                    clocal[pn] = nn
                    d.const_nets[nn] = n
            self._elab_module(child, d.cell_names[c] + "/", clocal, {})

    def _inst_stmt(self, mod, s, prefix, local, buses, children):
        d = self.d
        m = _RX_INST.match(s)
        if m is None:
            if s.strip(" \t\r\n;") and not _RX_DECL.match(s):
                d.issues.append(("DES-004", "%s: cannot parse statement '%s'"
                                 % (mod.name, " ".join(s.split())[:120])))
            return
        refname = m.group(1)
        if refname[0] == "\\":
            refname = refname[1:].rstrip()
        iname = m.group(2)
        if iname[0] == "\\":
            iname = iname[1:].rstrip()
        am = re.match(r"^\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]$", m.group(3) or "")
        if am:
            self._inst_array(mod, prefix + iname, int(am.group(1)), int(am.group(2)),
                             refname, m.group(4), prefix, local, buses, children)
            return
        if m.group(3):
            iname += m.group(3).replace(" ", "")
        full = prefix + iname
        ri = self.ref_for(refname)
        r = d.refs[ri]
        body = m.group(4)
        row = [UNCONN] * len(r.pins)
        get_local = local.get
        if body.lstrip().startswith("."):
            for pin, expr in _RX_NAMED.findall(body):
                if pin[0] == "\\":
                    pin = pin[1:].rstrip()
                i = r.pidx.get(pin)
                expr = expr.strip()
                if i is not None:
                    if not expr:
                        continue
                    n = get_local(expr)
                    if n is None:
                        if expr[0] == "\\":
                            n = get_local(expr[1:].rstrip())
                        if n is None:
                            bits = self._bits(expr, prefix, local, buses)
                            if not bits:
                                continue
                            if len(bits) != 1:
                                d.issues.append(("DES-004", "%s: 1-bit pin %s connected to %d "
                                                 "bits (%s); using the LSB"
                                                 % (full, pin, len(bits), expr[:60])))
                            n = bits[-1]
                    row[i] = n
                    continue
                # pin not a single bit of the ref: bus pin or unknown pin
                bits = self._bits(expr, prefix, local, buses) if expr else []
                self._bus_conn(r, row, pin, bits, full)
        elif body.strip():
            exprs = _split_top(body)
            if r.kind == REF_LIB:
                d.issues.append(("DES-004", "%s: positional connections on Liberty cell "
                                 "%s not supported (Liberty has no port order)" % (full, refname)))
            else:
                if r.kind != REF_MODULE:
                    d.issues.append(("DES-004", "%s: positional connections on unknown "
                                     "cell %s; pins named _p<N>" % (full, refname)))
                    for j in range(len(exprs)):
                        r.add_pin("_p%d" % j)
                    row.extend([UNCONN] * (len(r.pins) - len(row)))
                for j, ex in enumerate(exprs):
                    bits = self._bits(ex, prefix, local, buses)
                    if r.kind == REF_MODULE:
                        ports = self.modules[refname].ports
                        if j < len(ports):
                            self._bus_conn(r, row, ports[j], bits, full)
                    elif bits:
                        row[j] = bits[0]
        c = len(d.cell_names)
        d.cell_names.append(full)
        d.cell_index[full] = c
        d.cell_ref.append(ri)
        d.conn.extend(row)
        d.cell_off.append(len(d.conn))
        if r.kind == REF_MODULE:
            children.append((c, ri))

    def _inst_array(self, mod, base, a, b, refname, body, prefix, local, buses, children):
        """Instance array  REF u[a:b] (...): one cell per element. A connection
        whose width is N x pin width is sliced (leftmost element gets the MSBs);
        a pin-width connection is shared by all elements (Verilog rules)."""
        d = self.d
        ri = self.ref_for(refname)
        r = d.refs[ri]
        st = -1 if a > b else 1
        idxs = list(range(a, b + st, st))
        if not body.lstrip().startswith("."):
            d.issues.append(("DES-004", "%s[%d:%d]: positional connections on an instance "
                             "array are not supported; array not elaborated" % (base, a, b)))
            return
        conns = []
        for pin, expr in _RX_NAMED.findall(body):
            if pin[0] == "\\":
                pin = pin[1:].rstrip()
            conns.append((pin, self._bits(expr, prefix, local, buses) if expr.strip() else []))
        n = len(idxs)
        for k, ix in enumerate(idxs):
            full = "%s[%d]" % (base, ix)
            row = [UNCONN] * len(r.pins)
            for pin, bits in conns:
                w = len(r.bus[pin]) if pin in r.bus else 1
                if len(bits) == w * n and n > 1:
                    sl = bits[k * w:(k + 1) * w]
                elif len(bits) in (0, w):
                    sl = bits
                else:
                    if k == 0:
                        d.issues.append(("DES-004", "%s[%d:%d]: pin %s connected to %d bit(s); "
                                         "expected %d or %d" % (base, a, b, pin, len(bits), w, w * n)))
                    sl = bits[-w:]
                if pin in r.pidx and pin not in r.bus:
                    if sl:
                        row[r.pidx[pin]] = sl[-1]
                else:
                    self._bus_conn(r, row, pin, sl, full)
            c = len(d.cell_names)
            d.cell_names.append(full)
            d.cell_index[full] = c
            d.cell_ref.append(ri)
            d.conn.extend(row)
            d.cell_off.append(len(d.conn))
            if r.kind == REF_MODULE:
                children.append((c, ri))

    def _bus_conn(self, r, row, pin, bits, full):
        if pin in r.bus:
            idxs = r.bus[pin]
            if bits and len(bits) != len(idxs):
                self.d.issues.append(("DES-004", "%s: pin %s width %d connected to %d bit(s)"
                                      % (full, pin, len(idxs), len(bits))))
            for i, n in zip(idxs, bits):
                row[i] = n
            return
        # unknown pin
        if r.kind == REF_LIB and (r.name, pin) not in self.unknown_pin_warned:
            self.unknown_pin_warned.add((r.name, pin))
            self.d.issues.append(("DES-005", "pin %s not defined on Liberty cell %s (first seen on %s)"
                                  % (pin, r.name, full)))
        if len(bits) <= 1:
            i = r.add_pin(pin)
            row.extend([UNCONN] * (len(r.pins) - len(row)))
            if bits:
                row[i] = bits[0]
        else:
            w = len(bits)
            for k, n in enumerate(bits):
                i = r.add_pin("%s[%d]" % (pin, w - 1 - k))
                row.extend([UNCONN] * (len(r.pins) - len(row)))
                row[i] = n
        r.unknown_pins.add(pin)


def _instantiates(mod, name):
    """True if module body contains a statement starting with 'name' (C-speed find)."""
    text = mod.text
    bs, be = mod.body
    for tok in (name, "\\" + name):
        i = text.find(tok, bs, be)
        while i >= 0:
            j = i - 1
            while j >= bs and text[j] in _WS:
                j -= 1
            nxt = text[i + len(tok):i + len(tok) + 1]
            if (j < bs or text[j] == ";") and (nxt in _WS or nxt in "#("):
                return True
            i = text.find(tok, i + len(tok), be)
    return False


def build_design(netlists, libs, top, blackbox, jobs=1):
    """Parse netlists, resolve references with Liberty, elaborate from top."""
    t0 = time.time()
    d = Design()
    d.libs = libs
    modules = {}
    for path in netlists:
        if not os.path.isfile(path):
            _fatal("ERROR: netlist not found: %s" % path)
        for m in read_verilog_modules(path):
            if m.name in modules:
                d.issues.append(("DES-004", "module %s defined more than once (%s and %s); "
                                 "using the last" % (m.name, modules[m.name].file, path)))
            modules[m.name] = m
            for iss in m.issues:
                d.issues.append(("DES-004", iss))
    if not modules:
        _fatal("ERROR: no Verilog module found in %s" % ", ".join(netlists))
    _log("netlist read: %d module(s) in %.1fs" % (len(modules), time.time() - t0))
    # Liberty cells -> refs (first definition wins)
    lib_refs = {}
    sigs = {}
    tunits, cunits = set(), set()
    for lib in libs:
        if lib.get("time_unit"):
            tunits.add(lib["time_unit"])
        if lib.get("cap_unit"):
            cunits.add(lib["cap_unit"])
        for cname, cell in lib["cells"].items():
            if cname in lib_refs:
                if sigs[cname] != _pin_sig(cell):
                    d.issues.append(("LIB-002", "cell %s: pins/directions in %s differ from %s"
                                     % (cname, lib["path"], lib_refs[cname].lib)))
                continue
            sigs[cname] = _pin_sig(cell)
            lib_refs[cname] = ref_from_lib(cname, cell, lib["path"])
    d.time_unit = sorted(tunits)[0] if tunits else None
    d.cap_unit = sorted(cunits)[0] if cunits else None
    if len(tunits) > 1:
        d.issues.append(("UNIT-002", "Liberty time units differ: %s" % ", ".join(sorted(tunits))))
    if len(cunits) > 1:
        d.issues.append(("UNIT-002", "Liberty capacitance units differ: %s" % ", ".join(sorted(cunits))))
    d.lib_refs = lib_refs
    # top module
    if top is None:
        used = set()
        for name in modules:
            for om in modules.values():
                if om.name != name and _instantiates(om, name):
                    used.add(name)
                    break
        cands = [n for n in modules if n not in used]
        if len(cands) != 1:
            _fatal("ERROR: cannot determine top module (candidates: %s); use -top"
                             % ", ".join(sorted(cands)[:10]))
        top = cands[0]
    if top not in modules:
        _fatal("ERROR: top module %s not found in netlist(s)" % top)
    el = Elaborator(d, modules, lib_refs, blackbox, jobs)
    t1 = time.time()
    el.elaborate(top)
    for m in modules.values():
        m.text = None
    _log("elaborated %s: %d cells, %d nets, %d ports in %.1fs"
         % (top, len(d.cell_names), len(d.net_names), len(d.ports), time.time() - t1))
    t1 = time.time()
    d.build_driver_map()
    _log("driver map built in %.1fs" % (time.time() - t1))
    return d


# =============================================================================
# 4. Name matching (PrimeTime-style patterns)
#
# Exact names hit a dict. Wildcards run one compiled regex over a newline-joined
# "blob" of all names (the scan happens in C, ~50x faster than a Python loop
# over 2M names). Results are cached per pattern.
#   * and ? do not cross the hierarchy separator '/' for cells/pins/nets.
#   [ and ] are literal (bus bits), as in PrimeTime patterns.
#   -hierarchical matches the pattern against every trailing part of the name
#   that starts after a '/'.
# =============================================================================

def _glob_to_re(pat, hier, simple=False, flat=False):
    anyc = "." if simple else ("[^\n]" if flat else "[^/\n]")
    out = []
    for ch in pat:
        if ch == "*":
            out.append(anyc + "*")
        elif ch == "?":
            out.append(anyc)
        else:
            out.append(re.escape(ch))
    body = "".join(out)
    if simple:
        return "^" + body + "$"
    if hier:
        return "^(?:[^\n]*/)?(?:" + body + ")$"
    return "^(?:" + body + ")$"


class NameSpace(object):
    def __init__(self, names_fn, index, bus=None, flat=False):
        self._names_fn = names_fn      # callable -> iterable of names
        self.index = index             # name -> id
        self.bus = bus or {}           # bus base -> [ids]
        self.flat = flat
        self._blob = None
        self._bus_blob = None
        self._cache = {}

    def blob(self):
        if self._blob is None:
            self._blob = "\n".join(self._names_fn()) + "\n"
        return self._blob

    def match(self, pat, hier=False, regexp=False, nocase=False):
        key = (pat, hier, regexp, nocase)
        r = self._cache.get(key)
        if r is not None:
            return r
        r = self._match(pat, hier, regexp, nocase)
        if len(self._cache) > 20000:
            self._cache.clear()
        self._cache[key] = r
        return r

    def _match(self, pat, hier, regexp, nocase):
        if not regexp and not nocase and not hier and "*" not in pat and "?" not in pat:
            i = self.index.get(pat)
            if i is not None:
                return (i,)
            return tuple(self.bus.get(pat, ()))
        if regexp:
            rx = ("^(?:[^\n]*/)?(?:%s)$" if hier else "^(?:%s)$") % pat
        else:
            rx = _glob_to_re(pat, hier and not self.flat, flat=self.flat)
        try:
            cre = re.compile(rx, re.M | (re.I if nocase else 0))
        except re.error:
            return ()
        idx = self.index
        lit = "" if (regexp or nocase) else max(pat.replace("?", "*").split("*"), key=len)
        if len(lit) >= 3:
            # C-speed substring prefilter, regex only on candidate lines
            blob = self.blob()
            m1 = cre.match
            out = []
            i = blob.find(lit)
            while i >= 0:
                a = blob.rfind("\n", 0, i) + 1
                e = blob.find("\n", i)
                name = blob[a:e]
                if m1(name) and name in idx:
                    out.append(idx[name])
                i = blob.find(lit, e)
        else:
            out = [idx[n] for n in cre.findall(self.blob()) if n in idx]
        if self.bus:
            if self._bus_blob is None:
                self._bus_blob = "\n".join(self.bus) + "\n"
            seen = set(out)
            for b in cre.findall(self._bus_blob):
                for i in self.bus.get(b, ()):
                    if i not in seen:
                        seen.add(i)
                        out.append(i)
        return tuple(out)


def _pin_matcher(pat, regexp, nocase):
    if regexp:
        return re.compile("^(?:%s)$" % pat, re.I if nocase else 0)
    return re.compile(_glob_to_re(pat, False, flat=True), re.I if nocase else 0)


# =============================================================================
# 5. Filter expressions (PrimeTime -filter / filter_collection syntax)
#    attr op value, ops: == != =~ !~ < > <= >= ; && || ! ( ) ; defined(a)
#    undefined(a); leading '@' (DC style) accepted; bare attr = attr==true.
# =============================================================================

_RX_FTOK = re.compile(r'\s*(?:(\(|\)|&&|\|\||==|!=|=~|!~|<=|>=|<|>|!)|"((?:[^"\\]|\\.)*)"|([^\s()&|=!<>"]+))')


class FilterError(Exception):
    pass


class UnsupportedAttr(Exception):
    pass


def compile_filter(expr, getattr_fn, regexp=False, nocase=False, on_unsupported=None):
    toks = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _RX_FTOK.match(expr, pos)
        if not m or m.end() == pos:
            raise FilterError("cannot tokenize filter near '%s'" % expr[pos:pos + 20])
        pos = m.end()
        if m.group(1):
            toks.append(("op", m.group(1)))
        elif m.group(2) is not None:
            toks.append(("str", m.group(2)))
        elif m.group(3):
            w = m.group(3)
            lw = w.lower()
            if lw in ("and", "or"):
                toks.append(("op", "&&" if lw == "and" else "||"))
            else:
                toks.append(("word", w))
    toks.append(("end", None))
    st = {"i": 0}

    def peek():
        return toks[st["i"]]

    def nxt():
        t = toks[st["i"]]
        st["i"] += 1
        return t

    def parse_or():
        left = parse_and()
        while peek() == ("op", "||"):
            nxt()
            right = parse_and()
            left = (lambda a, b: lambda o: a(o) or b(o))(left, right)
        return left

    def parse_and():
        left = parse_not()
        while peek() == ("op", "&&"):
            nxt()
            right = parse_not()
            left = (lambda a, b: lambda o: a(o) and b(o))(left, right)
        return left

    def parse_not():
        if peek() == ("op", "!"):
            nxt()
            inner = parse_not()
            return lambda o: not inner(o)
        return parse_atom()

    def attr_value(attr, o):
        try:
            return getattr_fn(o, attr)
        except UnsupportedAttr:
            if on_unsupported:
                on_unsupported(attr)
            raise

    def parse_atom():
        t = nxt()
        if t == ("op", "("):
            e = parse_or()
            if nxt() != ("op", ")"):
                raise FilterError("missing ')'")
            return e
        if t[0] != "word":
            raise FilterError("unexpected token %r" % (t[1],))
        attr = t[1].lstrip("@")
        if attr in ("defined", "undefined") and peek() == ("op", "("):
            nxt()
            a2 = nxt()[1].lstrip("@")
            if nxt() != ("op", ")"):
                raise FilterError("missing ')'")
            want = attr == "defined"

            def f_def(o, a2=a2, want=want):
                try:
                    return (attr_value(a2, o) is not None) == want
                except UnsupportedAttr:
                    return True
            return f_def
        op = peek()
        if op[0] == "op" and op[1] in ("==", "!=", "=~", "!~", "<", ">", "<=", ">="):
            nxt()
            vt = nxt()
            if vt[0] not in ("word", "str"):
                raise FilterError("missing value after %s" % op[1])
            return _mk_cmp(attr, op[1], vt[1], attr_value, regexp, nocase)

        def f_bool(o, attr=attr):
            try:
                v = attr_value(attr, o)
            except UnsupportedAttr:
                return True
            return _truthy(v)
        return f_bool

    fn = parse_or()
    if peek()[0] != "end":
        raise FilterError("unexpected trailing text in filter")
    return fn


def _truthy(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).lower() in ("true", "1")


def _mk_cmp(attr, op, val, attr_value, regexp, nocase):
    if op in ("=~", "!~"):
        if regexp:
            rx = re.compile("^(?:%s)$" % val, re.I if nocase else 0)
        else:
            rx = re.compile(_glob_to_re(val, False, simple=True), re.I if nocase else 0)
    try:
        fval = float(val)
    except ValueError:
        fval = None
    lval = val.lower()

    def f(o):
        try:
            v = attr_value(attr, o)
        except UnsupportedAttr:
            return True
        if v is None:
            return op in ("!=", "!~")
        if isinstance(v, bool):
            sv = "true" if v else "false"
        else:
            sv = str(v)
        if op == "==":
            if fval is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v) == fval
            return sv == val if not nocase else sv.lower() == lval
        if op == "!=":
            if fval is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v) != fval
            return sv != val if not nocase else sv.lower() != lval
        if op == "=~":
            return rx.match(sv) is not None
        if op == "!~":
            return rx.match(sv) is None
        try:
            a, b = float(v), fval if fval is not None else float(val)
        except (TypeError, ValueError):
            return False
        return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
    return f


# =============================================================================
# 6. SDC engine: one fresh Tcl interpreter per mode
# =============================================================================

class Loc(object):
    __slots__ = ("file", "line", "cmd")

    def __init__(self, file, line, cmd):
        self.file, self.line, self.cmd = file, line, cmd


class Clock(object):
    __slots__ = ("name", "period", "waveform", "sources", "add", "gen", "loc", "virtual")

    def __init__(self, name, period, waveform, sources, add, gen, loc):
        self.name, self.period, self.waveform = name, period, waveform
        self.sources, self.add, self.gen, self.loc = sources, add, gen, loc
        self.virtual = not sources


class Coll(object):
    __slots__ = ("keys", "approx")

    def __init__(self, keys, approx=False):
        self.keys, self.approx = keys, approx


def _o(types):
    return ("obj", tuple(types.split(",")))


_EXC_VALS = {
    "-from": _o("clock,port,pin,cell"), "-rise_from": _o("clock,port,pin,cell"),
    "-fall_from": _o("clock,port,pin,cell"), "-to": _o("clock,port,pin,cell"),
    "-rise_to": _o("clock,port,pin,cell"), "-fall_to": _o("clock,port,pin,cell"),
    "-through": _o("port,pin,cell,net"), "-rise_through": _o("port,pin,cell,net"),
    "-fall_through": _o("port,pin,cell,net"), "-comment": "str",
}
_EXC_MULTI = ("-through", "-rise_through", "-fall_through")


def _spec(flags="", vals=None, pos=None, multi=(), kind="record"):
    return {"flags": set(flags.split()), "vals": vals or {}, "pos": pos, "multi": set(multi),
            "kind": kind}


# pos: (value_count, object_types or None, objects_required)
SDC_CMDS = {
    "create_clock": _spec("-add", {"-name": "str", "-period": "num", "-waveform": "str",
                                   "-comment": "str"}, (0, "port,pin", False)),
    "create_generated_clock": _spec(
        "-add -invert -preinvert -combinational",
        {"-name": "str", "-source": _o("port,pin"), "-edges": "str", "-divide_by": "num",
         "-multiply_by": "num", "-duty_cycle": "num", "-edge_shift": "str",
         "-master_clock": _o("clock"), "-comment": "str", "-pll_output": _o("pin"),
         "-pll_feedback": _o("pin")}, (0, "port,pin", True)),
    "set_clock_groups": _spec("-logically_exclusive -physically_exclusive -asynchronous "
                              "-allow_paths", {"-name": "str", "-group": _o("clock"),
                                               "-comment": "str"}, (0, None, False),
                              multi=("-group",)),
    "set_clock_latency": _spec("-rise -fall -min -max -source -early -late -dynamic",
                               {"-clock": _o("clock")}, (1, "clock,port,pin", True)),
    "set_clock_uncertainty": _spec("-setup -hold -rise -fall",
                                   {"-from": _o("clock"), "-to": _o("clock"),
                                    "-rise_from": _o("clock"), "-fall_from": _o("clock"),
                                    "-rise_to": _o("clock"), "-fall_to": _o("clock")},
                                   (1, "clock,port,pin", False)),
    "set_clock_transition": _spec("-rise -fall -min -max", {}, (1, "clock", True)),
    "set_propagated_clock": _spec("", {}, (0, "clock,port,pin", True)),
    "set_clock_sense": _spec("-positive -negative -stop_propagation -pulse",
                             {"-clocks": _o("clock"), "-clock": _o("clock")}, (0, "pin,port", True)),
    "set_sense": _spec("-positive -negative -stop_propagation -non_unate -clock_leaf",
                       {"-type": "str", "-clocks": _o("clock"), "-pulse": "str"},
                       (0, "pin,port", True)),
    "set_ideal_network": _spec("-no_propagate", {}, (0, "port,pin,net", True)),
    "set_ideal_latency": _spec("-rise -fall -min -max", {}, (1, "port,pin", True)),
    "set_ideal_transition": _spec("-rise -fall -min -max", {}, (1, "port,pin", True)),
    "set_input_delay": _spec("-clock_fall -level_sensitive -rise -fall -max -min -add_delay "
                             "-network_latency_included -source_latency_included",
                             {"-clock": _o("clock"), "-reference_pin": _o("pin,port")},
                             (1, "port,pin", True)),
    "set_output_delay": _spec("-clock_fall -level_sensitive -rise -fall -max -min -add_delay "
                              "-network_latency_included -source_latency_included",
                              {"-clock": _o("clock"), "-reference_pin": _o("pin,port")},
                              (1, "port,pin", True)),
    "set_false_path": _spec("-setup -hold -rise -fall", _EXC_VALS, (0, None, False),
                            multi=_EXC_MULTI),
    "set_multicycle_path": _spec("-setup -hold -rise -fall -start -end", _EXC_VALS,
                                 (1, None, False), multi=_EXC_MULTI),
    "set_max_delay": _spec("-rise -fall -ignore_clock_latency", _EXC_VALS, (1, None, False),
                           multi=_EXC_MULTI),
    "set_min_delay": _spec("-rise -fall -ignore_clock_latency", _EXC_VALS, (1, None, False),
                           multi=_EXC_MULTI),
    "group_path": _spec("-default", dict(_EXC_VALS, **{"-name": "str", "-weight": "num",
                                                       "-critical_range": "num"}),
                        (0, None, False), multi=_EXC_MULTI),
    "set_disable_timing": _spec("-restore", {"-from": "str", "-to": "str"},
                                (0, "cell,pin,port,lib_cell", True)),
    "set_case_analysis": _spec("", {}, (1, "port,pin", True)),
    "set_driving_cell": _spec("-rise -fall -min -max -dont_scale -no_design_rule -clock_fall",
                              {"-lib_cell": "str", "-library": "str", "-pin": "str",
                               "-from_pin": "str", "-multiply_by": "num", "-cell": "str",
                               "-input_transition_rise": "num", "-input_transition_fall": "num",
                               "-clock": _o("clock")}, (0, "port", True)),
    "set_drive": _spec("-rise -fall -min -max", {}, (1, "port", True)),
    "set_input_transition": _spec("-rise -fall -min -max -clock_fall", {"-clock": _o("clock")},
                                  (1, "port", True)),
    "set_load": _spec("-min -max -subtract_pin_load -pin_load -wire_load -rise -fall", {},
                      (1, "port,net", True)),
    "set_fanout_load": _spec("", {}, (1, "port", True)),
    "set_port_fanout_number": _spec("", {}, (1, "port", True)),
    "set_max_transition": _spec("-clock_path -data_path -rise -fall", {},
                                (1, "clock,port,design,pin", True)),
    "set_max_capacitance": _spec("-clock_path -data_path -rise -fall", {},
                                 (1, "clock,port,design,pin", True)),
    "set_min_capacitance": _spec("", {}, (1, "port,design,pin", True)),
    "set_max_fanout": _spec("", {}, (1, "port,design", True)),
    "set_timing_derate": _spec("-early -late -rise -fall -clock -data -net_delay -cell_delay "
                               "-cell_check -static -dynamic -increment -min -max", {},
                               (1, "cell,lib_cell,net", False)),
    "set_units": _spec("", {"-time": "str", "-capacitance": "str", "-resistance": "str",
                            "-voltage": "str", "-current": "str", "-power": "str"},
                       (0, None, False)),
    "set_min_pulse_width": _spec("-low -high", {}, (1, "clock,pin,cell", False)),
    "set_data_check": _spec("-setup -hold", {"-from": _o("pin"), "-to": _o("pin"),
                                             "-rise_from": _o("pin"), "-fall_from": _o("pin"),
                                             "-rise_to": _o("pin"), "-fall_to": _o("pin"),
                                             "-clock": _o("clock")}, (1, None, False)),
    "set_clock_gating_check": _spec("-rise -fall -high -low", {"-setup": "num", "-hold": "num"},
                                    (0, "clock,cell,pin", False)),
    "set_max_time_borrow": _spec("", {}, (1, "clock,cell,pin", True)),
    "set_resistance": _spec("-min -max", {}, (1, "net", True)),
    "set_logic_zero": _spec("", {}, (0, "port,pin", True)),
    "set_logic_one": _spec("", {}, (0, "port,pin", True)),
    "set_logic_dc": _spec("", {}, (0, "port,pin", True)),
    "set_dont_touch_network": _spec("-no_propagate", {}, (0, "clock,port,pin", True)),
    "set_hierarchy_separator": _spec("", {}, (1, None, False)),
    "set_wire_load_mode": _spec("", {}, (1, None, False)),
    "set_max_area": _spec("-ignore_tns", {}, (1, None, False)),
}

# Commands whose positional value is not a number
_NON_NUMERIC_VALUE = set(["set_case_analysis", "set_hierarchy_separator", "set_wire_load_mode"])

# Commands whose clock arguments do not count as "using" a (virtual) clock
_NOT_CLOCK_REFS = set("""set_propagated_clock set_clock_transition set_max_transition
set_max_capacitance set_dont_touch_network create_clock""".split())

# Accepted, not modelled (reported once per command as PARSE-005 INFO)
NOOP_CMDS = set("""
set_app_var set_app_options get_app_var suppress_message unsuppress_message set_message_info
echo printvar history date set_svf set_dont_use set_dont_touch set_operating_conditions
set_wire_load_model set_wire_load_selection_group set_max_dynamic_power set_max_leakage_power
set_leakage_optimization set_dynamic_optimization set_fix_hold set_cost_priority
set_critical_range set_voltage set_level_shifter_strategy set_level_shifter_threshold
set_design_attributes set_user_attribute define_user_attribute set_attribute remove_clock
remove_generated_clock remove_input_delay remove_output_delay remove_clock_groups
remove_case_analysis remove_clock_latency remove_clock_uncertainty reset_path reset_design
set_fix_multiple_port_nets set_isolate_ports set_flatten set_structure set_max_net_length
set_clock_gating_style set_scope set_related_supply_net set_port_attributes
set_load_unit update_timing report_timing report_clock check_timing set_ideal_net
set_timing_derate_mode set_min_library set_disable_clock_gating_check set_annotated_delay
set_annotated_check set_annotated_transition read_parasitics set_input_parasitics
""".split())

# Master-side helper exposed to the SDC as 'file': path-string subcommands only
_TCL_MASTER_SETUP = r"""
proc __qc_file_safe {sub args} {
    set ok {dirname tail rootname extension join split normalize nativename pathtype}
    if {[lsearch -exact $ok $sub] < 0} { error "sdc_qc sandbox: 'file $sub' is disabled" }
    return [file $sub {*}$args]
}
"""

_TCL_SETUP = r"""
proc exit args { error "exit called in SDC" }
proc puts args { return "" }
proc __qc_run {__qc_lvl __qc_s} {
    set __qc_c [catch {uplevel #$__qc_lvl $__qc_s} __qc_m __qc_o]
    if {$__qc_c == 1} { return [list 1 $__qc_m [dict get $__qc_o -errorinfo]] }
    return [list $__qc_c $__qc_m ""]
}
proc foreach_in_collection {__qc_var __qc_coll __qc_body} {
    upvar 1 $__qc_var __qc_v
    foreach __qc_e [__qc_split $__qc_coll] {
        set __qc_v $__qc_e
        set __qc_c [catch {uplevel 1 $__qc_body} __qc_m __qc_o]
        switch -- $__qc_c {
            0 {} 1 { return -options $__qc_o $__qc_m } 2 { return -code return $__qc_m }
            3 { break } 4 { continue } default { return -code $__qc_c $__qc_m }
        }
    }
    return ""
}
proc append_to_collection {__qc_var args} {
    upvar 1 $__qc_var __qc_v
    if {![info exists __qc_v]} { set __qc_v "" }
    set __qc_v [add_to_collection $__qc_v {*}$args]
    return $__qc_v
}
proc redirect {args} {
    set __qc_body [lindex $args end]
    set __qc_i [lsearch -exact $args -variable]
    set __qc_r [uplevel 1 $__qc_body]
    if {$__qc_i >= 0} { upvar 1 [lindex $args [expr {$__qc_i + 1}]] __qc_rv; set __qc_rv $__qc_r }
    return ""
}
proc define_proc_attributes {name args} {
    set i [lsearch -exact $args -define_args]
    if {$i >= 0} { set ::__qc_pa([string trimleft $name :]) [lindex $args [expr {$i + 1}]] }
    return ""
}
proc parse_proc_arguments {args} {
    set i [lsearch -exact $args -args]
    set pargs [lindex $args [expr {$i + 1}]]
    upvar 1 [lindex $args end] res
    set caller [string trimleft [lindex [info level -1] 0] :]
    set spec {}
    if {[info exists ::__qc_pa($caller)]} { set spec $::__qc_pa($caller) }
    set opts {}; set posn {}
    foreach s $spec {
        set n [lindex $s 0]; set vt [lindex $s 3]
        if {[string index $n 0] eq "-"} { dict set opts $n [expr {$vt eq "boolean" || [lindex $s 2] eq ""}] } else { lappend posn $n }
    }
    set k 0
    for {set j 0} {$j < [llength $pargs]} {incr j} {
        set a [lindex $pargs $j]
        if {[dict exists $opts $a]} {
            if {[dict get $opts $a]} { set res($a) 1 } else { incr j; set res($a) [lindex $pargs $j] }
        } elseif {$k < [llength $posn]} {
            set res([lindex $posn $k]) $a; incr k
        }
    }
    return 1
}
"""


class ModeState(object):
    def __init__(self, name):
        self.name = name
        self.clocks = collections.OrderedDict()
        self.clock_defs = []            # every create_*clock record, in order
        self.io = []                    # I/O delay records
        self.exceptions = []
        self.clock_groups = []
        self.case = {}                  # key -> (value, loc)
        self.drive_ports, self.load_ports = set(), set()
        self.units = {}
        self.clock_refs = set()
        self.records = []               # other constraint records
        self.current_design = None
        self.propagated = []
        self.disable = []
        self.files = []
        self.seconds = 0.0


def _is_float(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _num(s):
    """Finite float or None (rejects NaN/Inf: never valid timing values)."""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class SdcEngine(object):
    def __init__(self, design, mode_name, opts):
        import tkinter
        self.d = design
        self.opts = opts
        self.m = ModeState(mode_name)
        self.findings = []
        self._fkeys = set()
        self.colls = {}
        self._ncoll = 0
        self.file_stack = []           # [(path, lines, chunk_start_line)]
        self.cur_instance = ""
        self._noop_seen = {}
        self._unknown_seen = {}
        self._unsupported_attr = set()
        self.tracer = None
        self.tcl = tkinter.Tcl()
        self._ns = None
        self._setup_tcl()

    # ------------------------------------------------------------------ infra
    def sdc_eval(self, script):
        """Evaluate a script in the sandboxed child interpreter that runs the SDC."""
        return self.tcl.call("interp", "eval", "sdc", script)

    def _setup_tcl(self):
        # The SDC runs in a Tcl *safe* child interpreter: open/file/exec/socket/
        # cd/load/source/glob are hidden there and cannot be reached from the
        # SDC (a safe interp cannot invoke its hidden commands). Python
        # commands live in the master and are exposed through aliases.
        t = self.tcl
        t.call("interp", "create", "-safe", "sdc")
        t.eval(_TCL_MASTER_SETUP)
        t.call("interp", "alias", "sdc", "file", "", "__qc_file_safe")
        self.sdc_eval(_TCL_SETUP)
        py = {
            "get_ports": self.c_get_ports, "get_pins": self.c_get_pins,
            "get_cells": self.c_get_cells, "get_nets": self.c_get_nets,
            "get_clocks": self.c_get_clocks, "get_lib_cells": self.c_get_lib_cells,
            "get_lib_pins": self.c_get_lib_pins, "get_libs": self.c_get_libs,
            "get_designs": self.c_get_designs, "current_design": self.c_current_design,
            "current_instance": self.c_current_instance,
            "all_inputs": self.c_all_inputs, "all_outputs": self.c_all_outputs,
            "all_clocks": self.c_all_clocks, "all_registers": self.c_all_registers,
            "all_fanin": self.c_all_fan, "all_fanout": self.c_all_fan,
            "sizeof_collection": self.c_sizeof, "get_object_name": self.c_get_object_name,
            "add_to_collection": self.c_add_to_collection,
            "remove_from_collection": self.c_remove_from_collection,
            "filter_collection": self.c_filter_collection,
            "index_collection": self.c_index_collection,
            "sort_collection": self.c_passthrough, "copy_collection": self.c_passthrough,
            "get_attribute": self.c_get_attribute, "get_property": self.c_get_attribute,
            "query_objects": self.c_query_objects, "unknown": self.c_unknown,
        }
        for name in SDC_CMDS:
            py[name] = (lambda n: lambda frame, *a: self.c_constraint(n, frame, a))(name)
        for name in NOOP_CMDS:
            if name not in py:
                py[name] = (lambda n: lambda frame, *a: self.c_noop(n, frame, a))(name)
        py["source_lvl"] = self.c_source
        for name, fn in py.items():
            t.createcommand("__py_" + name, self._guard(name, fn))
            t.call("interp", "alias", "sdc", "__py_" + name, "", "__py_" + name)
            if name != "source_lvl":
                # Wrapper passes the caller's frame for file:line attribution
                self.sdc_eval("proc %s args { __py_%s [info frame -1] {*}$args }" % (name, name))
        t.createcommand("__qc_split", self.c_split)
        t.call("interp", "alias", "sdc", "__qc_split", "", "__qc_split")
        self.sdc_eval("proc source args { __py_source_lvl [info frame -1] "
                      "[expr {[info level]-1}] {*}$args }")

    def _guard(self, name, fn):
        def run(*a):
            try:
                return fn(*a)
            except Exception as e:
                loc = self.loc(a[0]) if a else None
                msg = str(e).split("\n")[0][:200]
                self.add("PARSE-001", "%s: cannot evaluate arguments (%s)" % (name, msg), loc)
                return ""
        return run

    def loc(self, frame):
        f, lines, start = self.file_stack[-1] if self.file_stack else ("<cmdline>", [], 1)
        line, cmd = start, ""
        try:
            items = self.tcl.splitlist(frame)
            fd = dict(zip(items[0::2], items[1::2]))
            cmd = " ".join(str(fd.get("cmd", "")).split())[:240]
            if fd.get("type") == "eval" and fd.get("proc") == "::__qc_run":
                cand = start + int(fd.get("line", 1)) - 1
                # verify: the command's first word should appear on that line
                w = cmd.split(" ", 1)[0] if cmd else ""
                if 0 < cand <= len(lines) and (not w or w in lines[cand - 1]):
                    line = cand
        except Exception:
            pass
        return Loc(f, line, cmd)

    def add(self, rule, msg, loc=None, obj="", sev=None):
        sev = sev or RULES[rule][0]
        f = loc.file if loc else ""
        ln = loc.line if loc else 0
        key = (rule, f, ln, msg)
        if key in self._fkeys:
            return
        self._fkeys.add(key)
        self.findings.append(Finding(rule, sev, self.m.name, f, ln, loc.cmd if loc else "",
                                     obj, msg))

    def new_coll(self, keys, approx=False):
        if not keys and not approx:
            return ""
        self._ncoll += 1
        h = "_sel%d" % self._ncoll
        self.colls[h] = Coll(keys, approx)
        return h

    def ns(self):
        if self._ns is None:
            d = self.d
            self._ns = {
                "port": NameSpace(lambda: (p.name for p in d.ports), d.port_index, d.port_bus,
                                  flat=True),
                "cell": NameSpace(lambda: d.cell_names, d.cell_index),
                "net": NameSpace(lambda: d.net_index.keys(), d.net_index, d.net_bus),
            }
        return self._ns

    # ----------------------------------------------------------- arg parsing
    def parse_args(self, cmd, args, flags, vals, multi, loc):
        opts, pos = {}, []
        names = sorted(set(flags) | set(vals))
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith("-") and len(a) > 1 and not _is_float(a):
                name = a if a in flags or a in vals else None
                if name is None:
                    cands = [n for n in names if n.startswith(a)]
                    if len(cands) == 1:
                        name = cands[0]
                if name is None:
                    self.add("PARSE-003", "%s: unknown option %s" % (cmd, a), loc)
                    i += 1
                    continue
                if name in flags:
                    opts[name] = True
                    i += 1
                    continue
                if i + 1 >= len(args):
                    self.add("PARSE-003", "%s: option %s needs a value" % (cmd, name), loc)
                    i += 1
                    continue
                v = args[i + 1]
                if name in multi:
                    opts.setdefault(name, []).append(v)
                else:
                    opts[name] = v
                i += 2
                continue
            pos.append(a)
            i += 1
        return opts, pos

    def _items(self, arg):
        out = []
        for it in self.tcl.splitlist(arg):
            if it in self.colls or it.startswith("_sel"):
                out.append(it)
            elif (" " in it or "\t" in it or "\n" in it) and it.strip():
                out.extend(self._items(it))
            elif it:
                out.append(it)
        return out

    def resolve(self, arg, types, loc, what):
        """Tcl value (collection handles and/or name patterns) -> (keys, approx)."""
        keys, approx = [], False
        for it in self._items(arg):
            c = self.colls.get(it)
            if c is not None:
                approx = approx or c.approx
                bad = [k for k in c.keys if k[0] not in types]
                if bad:
                    self.add("OBJ-003", "%s: %d object(s) of type %s not allowed (allowed: %s)"
                             % (what, len(bad), bad[0][0], ", ".join(types)), loc,
                             obj=self.obj_name(bad[0]))
                keys.extend(k for k in c.keys if k[0] in types)
                continue
            if it.startswith("_sel"):
                self.add("OBJ-003", "%s: stale or foreign collection handle %s" % (what, it), loc)
                continue
            found = self.implicit(it, types)
            if found is None:
                self.add("OBJ-001", "%s: '%s' matched no %s" % (what, it, "/".join(types)), loc,
                         obj=it)
            else:
                keys.extend(found)
        return keys, approx

    def implicit(self, pat, types):
        for t in types:
            if t == "clock":
                r = self.match_clocks(pat)
            elif t == "port":
                r = [("port", i) for i in self.ns()["port"].match(pat)]
            elif t == "cell":
                r = [("cell", i) for i in self.ns()["cell"].match(self._rel(pat))]
            elif t == "pin":
                r = self.match_pins(self._rel(pat), False, False, False)
            elif t == "net":
                r = [("net", i) for i in self.ns()["net"].match(self._rel(pat))]
            elif t == "design":
                r = [("design", self.d.top)] if pat == self.d.top else []
            elif t == "lib_cell":
                r = self.match_lib_cells(pat, False, False)
            else:
                r = []
            if r:
                return r
        return None

    def _rel(self, pat):
        return (self.cur_instance + "/" + pat) if self.cur_instance else pat

    # ------------------------------------------------------------- matching
    def match_clocks(self, pat, regexp=False, nocase=False):
        if not regexp and not nocase and "*" not in pat and "?" not in pat:
            return [("clock", pat)] if pat in self.m.clocks else []
        rx = re.compile(("^(?:%s)$" % pat) if regexp else _glob_to_re(pat, False, simple=True),
                        re.I if nocase else 0)
        return [("clock", n) for n in self.m.clocks if rx.match(n)]

    def match_pins(self, pat, hier, regexp, nocase, leaf=False):
        d = self.d
        if "/" in pat:
            cp, pp = pat.rsplit("/", 1)
        elif hier:
            cp, pp = "*", pat
        else:
            return []
        if not regexp and not nocase and not hier and "*" not in pat and "?" not in pat:
            c = d.cell_index.get(cp)
            if c is None:
                return []
            r = d.refs[d.cell_ref[c]]
            i = r.pidx.get(pp)
            if i is not None:
                return [("pin", c, i)]
            return [("pin", c, j) for j in r.bus.get(pp, ())]
        cells = self.ns()["cell"].match(cp, hier, regexp, nocase)
        prx = _pin_matcher(pp, regexp, nocase)
        memo = {}
        out = []
        refs, cref = d.refs, d.cell_ref
        for c in cells:
            ri = cref[c]
            idxs = memo.get(ri)
            if idxs is None:
                r = refs[ri]
                if leaf and r.kind == REF_MODULE:
                    idxs = ()
                else:
                    s = [j for j, n in enumerate(r.pins) if prx.match(n)]
                    for b, bits in r.bus.items():
                        if prx.match(b):
                            s.extend(j for j in bits if j not in s)
                    idxs = tuple(s)
                memo[ri] = idxs
            for j in idxs:
                out.append(("pin", c, j))
        return out

    def match_lib_cells(self, pat, regexp, nocase):
        if "/" in pat:
            lp, cp = pat.split("/", 1)
        else:
            lp, cp = "*", pat
        lrx = _pin_matcher(lp, regexp, nocase)
        crx = _pin_matcher(cp, regexp, nocase)
        out = []
        for lib in self.d.libs:
            if lib.get("name") and lrx.match(lib["name"]):
                for c in lib["cells"]:
                    if crx.match(c):
                        out.append(("lib_cell", lib["name"], c))
        return out

    # --------------------------------------------------------- object names
    def obj_name(self, k):
        t = k[0]
        d = self.d
        if t == "port":
            return d.ports[k[1]].name
        if t == "cell":
            return d.cell_names[k[1]]
        if t == "pin":
            return d.pin_name(k[1], k[2])
        if t == "net":
            return d.net_names[k[1]]
        if t == "clock" or t == "design":
            return k[1]
        if t == "lib_cell":
            return "%s/%s" % (k[1], k[2])
        if t == "lib_pin":
            return "%s/%s/%s" % (k[1], k[2], k[3])
        if t == "lib":
            return k[1]
        return str(k)

    def attr(self, k, a):
        t = k[0]
        d = self.d
        if a == "full_name":
            return self.obj_name(k)
        if a == "object_class":
            return t
        if t == "port":
            p = d.ports[k[1]]
            if a == "name":
                return p.name
            if a in ("direction", "port_direction"):
                return p.dir
            if a == "is_clock_source":
                return any(("port", k[1]) in c.sources for c in self.m.clocks.values())
        elif t == "cell":
            c = k[1]
            r = d.refs[d.cell_ref[c]]
            if a == "name":
                return d.cell_names[c].rsplit("/", 1)[-1]
            if a == "ref_name":
                return r.name
            if a == "is_hierarchical":
                return r.kind == REF_MODULE
            if a == "is_sequential":
                return bool(r.seq)
            if a == "is_combinational":
                return r.kind == REF_LIB and not r.seq
            if a == "is_black_box":
                return r.kind in (REF_BLACKBOX, REF_UNRESOLVED)
            if a == "is_integrated_clock_gating_cell":
                return r.icg_in >= 0
            if a == "is_memory_cell":
                return r.memory
            if a in ("is_macro_cell", "is_macro"):
                return r.macro
            if a == "is_edge_triggered":
                return r.seq == "ff"
            if a == "is_level_sensitive":
                return r.seq == "latch"
            if a == "is_buffer":
                return r.buf_in >= 0 and not r.inv
            if a == "is_inverter":
                return r.buf_in >= 0 and r.inv
        elif t == "pin":
            c, p = k[1], k[2]
            r = d.refs[d.cell_ref[c]]
            if a in ("name", "lib_pin_name"):
                return r.pins[p]
            if a in ("direction", "pin_direction"):
                return r.pdir[p]
            if a == "is_hierarchical":
                return r.kind == REF_MODULE
            if a == "is_clock_pin":
                return p in r.clock_pins
            if a == "is_data_pin":
                return p in r.endpoint_pins
            if a == "is_port":
                return False
            if a == "cell_name":
                return d.cell_names[c]
            if a == "ref_name":
                return r.name
        elif t == "net":
            if a == "name":
                return d.net_names[k[1]].rsplit("/", 1)[-1]
        elif t == "clock":
            ck = self.m.clocks.get(k[1])
            if a == "name":
                return k[1]
            if ck is None:
                return None
            if a == "period":
                return ck.period
            if a == "is_generated":
                return ck.gen is not None
            if a == "is_virtual":
                return ck.virtual
            if a == "sources":
                return self.new_coll(list(ck.sources))
            if a == "waveform":
                return " ".join("%g" % w for w in ck.waveform)
            if a == "master_clock":
                return (ck.gen or {}).get("master")
        elif t == "lib_cell":
            if a in ("name", "base_name"):
                return k[2]
            if a == "is_sequential":
                r = d.lib_refs.get(k[2])
                return bool(r and r.seq)
        elif t == "design":
            if a == "name":
                return k[1]
        raise UnsupportedAttr(a)

    def _unsupported(self, a, loc):
        self.add("PARSE-006", "attribute '%s' not modelled; filter treated as true / value "
                 "empty (results may differ from the STA tool)" % a, loc)

    def _filter(self, keys, expr, loc, regexp=False, nocase=False):
        try:
            fn = compile_filter(expr, self.attr, regexp, nocase,
                                on_unsupported=lambda a: self._unsupported(a, loc))
        except FilterError as e:
            self.add("PARSE-003", "bad filter expression '%s': %s" % (expr, e), loc)
            return keys
        return [k for k in keys if fn(k)]

    # ---------------------------------------------------------- get_* cmds
    _GET_FLAGS = "-hierarchical -quiet -regexp -nocase -exact -leaf -include_generated_clocks " \
                 "-top_net_of_hierarchical_group -segments"
    _GET_VALS = {"-filter": "str", "-of_objects": "str", "-hsc": "str"}

    def _get(self, cmd, frame, args, otype):
        loc = self.loc(frame)
        opts, pos = self.parse_args(cmd, args, set(self._GET_FLAGS.split()), self._GET_VALS,
                                    (), loc)
        hier = "-hierarchical" in opts
        regexp, nocase, quiet = "-regexp" in opts, "-nocase" in opts, "-quiet" in opts
        keys, approx = [], False
        if "-of_objects" in opts:
            src, approx = self.resolve(opts["-of_objects"],
                                       ("port", "cell", "pin", "net", "clock", "lib_cell"),
                                       loc, "%s -of_objects" % cmd)
            keys = self._of(otype, src, loc, cmd, "-leaf" in opts)
        else:
            pats = []
            for p in pos:
                pats.extend(self._items(p))
            if not pats:
                pats = ["*"]
            for p in pats:
                c = self.colls.get(p)
                if c is not None:
                    keys.extend(k for k in c.keys if k[0] == otype)
                    approx = approx or c.approx
                    continue
                r = self._match(otype, p, hier, regexp, nocase, "-leaf" in opts)
                if not r and not approx:
                    self.add("OBJ-001", "%s: '%s' matched no %s%s" % (cmd, p, otype,
                             " (hierarchical search)" if hier else ""), loc, obj=p,
                             sev=SEV_WARNING if quiet else None)
                keys.extend(r)
        if "-filter" in opts and keys:
            keys = self._filter(keys, opts["-filter"], loc, regexp, nocase)
        if len(keys) > 1:
            keys = list(collections.OrderedDict.fromkeys(keys))
        return self.new_coll(keys, approx)

    def _match(self, otype, p, hier, regexp, nocase, leaf):
        if otype == "port":
            return [("port", i) for i in self.ns()["port"].match(p, False, regexp, nocase)]
        if otype == "cell":
            return [("cell", i) for i in self.ns()["cell"].match(self._rel(p), hier, regexp, nocase)]
        if otype == "pin":
            return self.match_pins(self._rel(p), hier, regexp, nocase, leaf)
        if otype == "net":
            return [("net", i) for i in self.ns()["net"].match(self._rel(p), hier, regexp, nocase)]
        if otype == "clock":
            return self.match_clocks(p, regexp, nocase)
        if otype == "lib_cell":
            return self.match_lib_cells(p, regexp, nocase)
        if otype == "lib_pin":
            parts = p.split("/")
            if len(parts) < 2:
                return []
            pp = parts[-1]
            prx = _pin_matcher(pp, regexp, nocase)
            out = []
            for lk in self.match_lib_cells("/".join(parts[:-1]), regexp, nocase):
                r = self.d.lib_refs.get(lk[2])
                if r:
                    out.extend(("lib_pin", lk[1], lk[2], n) for n in r.pins if prx.match(n))
            return out
        if otype == "lib":
            rx = _pin_matcher(p, regexp, nocase)
            return [("lib", l["name"]) for l in self.d.libs if l.get("name") and rx.match(l["name"])]
        return []

    def _of(self, otype, src, loc, cmd, leaf):
        d = self.d
        out = []
        for k in src:
            t = k[0]
            if otype == "pin":
                if t == "cell":
                    c = k[1]
                    r = d.refs[d.cell_ref[c]]
                    out.extend(("pin", c, j) for j in range(len(r.pins)))
                elif t == "net":
                    for c, p in d.net_pins(k[1]):
                        if not (leaf and d.refs[d.cell_ref[c]].kind == REF_MODULE):
                            out.append(("pin", c, p))
                else:
                    self.add("OBJ-003", "%s -of_objects: %s objects not supported" % (cmd, t), loc)
            elif otype == "cell":
                if t == "pin":
                    out.append(("cell", k[1]))
                elif t == "net":
                    out.extend(("cell", c) for c, p in d.net_pins(k[1]))
                elif t == "lib_cell":
                    ri = d.ref_index.get(k[2])
                    if ri is not None:
                        out.extend(("cell", c) for c in range(len(d.cell_names)) if d.cell_ref[c] == ri)
                else:
                    self.add("OBJ-003", "%s -of_objects: %s objects not supported" % (cmd, t), loc)
            elif otype == "net":
                if t == "pin":
                    n = d.conn[d.cell_off[k[1]] + k[2]] if k[2] < d.ncell_pins(k[1]) else -1
                    if n >= 0:
                        out.append(("net", d.find(n)))
                elif t == "port":
                    n = d.ports[k[1]].net
                    if n >= 0:
                        out.append(("net", d.find(n)))
                elif t == "cell":
                    base = d.cell_off[k[1]]
                    for s in range(base, d.cell_off[k[1] + 1]):
                        if d.conn[s] >= 0:
                            out.append(("net", d.find(d.conn[s])))
                else:
                    self.add("OBJ-003", "%s -of_objects: %s objects not supported" % (cmd, t), loc)
            elif otype == "port":
                if t == "net":
                    rn = d.find(k[1])
                    out.extend(("port", i) for i, p in enumerate(d.ports)
                               if p.net >= 0 and d.find(p.net) == rn)
                else:
                    self.add("OBJ-003", "%s -of_objects: %s objects not supported" % (cmd, t), loc)
            elif otype == "lib_cell":
                if t == "cell":
                    r = d.refs[d.cell_ref[k[1]]]
                    if r.kind == REF_LIB:
                        lib = next((l["name"] for l in d.libs if l["path"] == r.lib), r.lib)
                        out.append(("lib_cell", lib, r.name))
                else:
                    self.add("OBJ-003", "%s -of_objects: %s objects not supported" % (cmd, t), loc)
            else:
                self.add("OBJ-003", "%s -of_objects not supported" % cmd, loc)
        return out

    def c_get_ports(self, frame, *a):
        return self._get("get_ports", frame, a, "port")

    def c_get_pins(self, frame, *a):
        return self._get("get_pins", frame, a, "pin")

    def c_get_cells(self, frame, *a):
        return self._get("get_cells", frame, a, "cell")

    def c_get_nets(self, frame, *a):
        return self._get("get_nets", frame, a, "net")

    def c_get_clocks(self, frame, *a):
        return self._get("get_clocks", frame, a, "clock")

    def c_get_lib_cells(self, frame, *a):
        return self._get("get_lib_cells", frame, a, "lib_cell")

    def c_get_lib_pins(self, frame, *a):
        return self._get("get_lib_pins", frame, a, "lib_pin")

    def c_get_libs(self, frame, *a):
        return self._get("get_libs", frame, a, "lib")

    def c_get_designs(self, frame, *a):
        return self.new_coll([("design", self.d.top)])

    def c_current_design(self, frame, *a):
        loc = self.loc(frame)
        if a:
            name = a[0]
            c = self.colls.get(name)
            if c is not None and c.keys:
                name = c.keys[0][1]
            self.m.current_design = name
            if name != self.d.top:
                self.add("DES-001", "current_design %s but netlist top is %s" % (name, self.d.top),
                         loc)
        return self.new_coll([("design", self.d.top)])

    def c_current_instance(self, frame, *a):
        loc = self.loc(frame)
        if not a or a[0] in ("", "/", "."):
            self.cur_instance = ""
            return ""
        inst = a[0]
        if inst == "..":
            self.cur_instance = self.cur_instance.rsplit("/", 1)[0] if "/" in self.cur_instance else ""
            return self.cur_instance
        full = self._rel(inst)
        c = self.d.cell_index.get(full)
        if c is None or self.d.refs[self.d.cell_ref[c]].kind != REF_MODULE:
            self.add("OBJ-001", "current_instance: hierarchical cell '%s' not found" % full, loc,
                     obj=full)
            return ""
        self.cur_instance = full
        return full

    def c_all_inputs(self, frame, *a):
        return self._all_io(frame, a, ("in", "inout"), "all_inputs")

    def c_all_outputs(self, frame, *a):
        return self._all_io(frame, a, ("out", "inout"), "all_outputs")

    def _all_io(self, frame, a, dirs, cmd):
        loc = self.loc(frame)
        opts, pos = self.parse_args(cmd, a, {"-no_clocks", "-edge_triggered",
                                             "-level_sensitive"}, {"-clock": "str"}, (), loc)
        keys = [("port", i) for i, p in enumerate(self.d.ports) if p.dir in dirs]
        if "-no_clocks" in opts:
            src = set()
            for c in self.m.clocks.values():
                src.update(c.sources)
            keys = [k for k in keys if k not in src]
        if "-clock" in opts:
            cks, _ = self.resolve(opts["-clock"], ("clock",), loc, cmd + " -clock")
            names = set(k[1] for k in cks)
            want = set()
            for r in self.m.io:
                if r["clock"] in names and r["kind"] == ("in" if "in" in dirs else "out"):
                    want.update(r["ports"])
            keys = [k for k in keys if k[1] in want]
        return self.new_coll(keys)

    def c_all_clocks(self, frame, *a):
        return self.new_coll([("clock", n) for n in self.m.clocks])

    def c_all_registers(self, frame, *a):
        loc = self.loc(frame)
        opts, pos = self.parse_args("all_registers", a, {
            "-clock_pins", "-data_pins", "-output_pins", "-edge_triggered", "-level_sensitive",
            "-no_hierarchy", "-cells", "-async_pins", "-master_slave", "-inverted_output"},
            {"-clock": "str", "-rise_clock": "str", "-fall_clock": "str", "-hsc": "str"}, (), loc)
        d = self.d
        want_ck = None
        for o in ("-clock", "-rise_clock", "-fall_clock"):
            if o in opts:
                cks, _ = self.resolve(opts[o], ("clock",), loc, "all_registers " + o)
                want_ck = (want_ck or set()) | set(k[1] for k in cks)
        seq_refs = []
        for ri, r in enumerate(d.refs):
            # ICGs are clock-gating cells, not registers (assumption; see README)
            if r.kind == REF_LIB and r.seq and r.icg_in < 0:
                if "-edge_triggered" in opts and r.seq != "ff":
                    continue
                if "-level_sensitive" in opts and r.seq != "latch":
                    continue
                seq_refs.append(ri)
        seq_set = set(seq_refs)
        cref = d.cell_ref
        cells = [c for c in range(len(d.cell_names)) if cref[c] in seq_set]
        if want_ck is not None:
            tr = self.get_tracer()
            keep = []
            for c in cells:
                r = d.refs[cref[c]]
                for p in r.clock_pins:
                    res = tr.trace_slot(d.cell_off[c] + p)
                    if res[0] == "clk" and res[1] & want_ck:
                        keep.append(c)
                        break
            cells = keep
        keys = []
        pin_sel = [o for o in ("-clock_pins", "-data_pins", "-output_pins") if o in opts]
        if not pin_sel:
            keys = [("cell", c) for c in cells]
        else:
            for c in cells:
                r = d.refs[cref[c]]
                if "-clock_pins" in opts:
                    keys.extend(("pin", c, p) for p in sorted(r.clock_pins))
                if "-data_pins" in opts:
                    keys.extend(("pin", c, p) for p in sorted(r.endpoint_pins))
                if "-output_pins" in opts:
                    keys.extend(("pin", c, p) for p in r.out_pins)
        return self.new_coll(keys)

    def c_all_fan(self, frame, *a):
        loc = self.loc(frame)
        self.add("PARSE-007", "all_fanin/all_fanout is deferred to a later release; returns an "
                 "unknown collection (object checks on it are skipped)", loc)
        return self.new_coll([], approx=True)

    # ------------------------------------------------------ collection cmds
    def c_split(self, arg):
        out = []
        for it in self._items(arg):
            c = self.colls.get(it)
            if c is None:
                out.append(it)
                continue
            for k in c.keys:
                out.append(self.new_coll([k]))
        return tuple(out)

    def _keys_of(self, arg):
        keys, approx = [], False
        for it in self._items(arg):
            c = self.colls.get(it)
            if c is not None:
                keys.extend(c.keys)
                approx = approx or c.approx
        return keys, approx

    def c_sizeof(self, frame, *a):
        return str(len(self._keys_of(a[0])[0])) if a else "0"

    def c_get_object_name(self, frame, *a):
        keys = self._keys_of(a[0])[0] if a else []
        if len(keys) == 1:
            return self.obj_name(keys[0])
        return tuple(self.obj_name(k) for k in keys)

    def c_query_objects(self, frame, *a):
        return ""

    def c_passthrough(self, frame, *a):
        return a[0] if a else ""

    def c_add_to_collection(self, frame, *a):
        loc = self.loc(frame)
        opts, pos = self.parse_args("add_to_collection", a, {"-unique"}, {}, (), loc)
        if not pos:
            return ""
        keys, approx = self._keys_of(pos[0])
        for p in pos[1:]:
            k2, ap2 = self._keys_of(p)
            if not k2 and not ap2:
                # names are resolved like PrimeTime: same class as the base collection
                types = tuple(sorted(set(k[0] for k in keys))) or ("port", "cell", "pin", "net", "clock")
                k2, ap2 = self.resolve(p, types, loc, "add_to_collection")
            keys.extend(k2)
            approx = approx or ap2
        if "-unique" in opts:
            keys = list(collections.OrderedDict.fromkeys(keys))
        return self.new_coll(keys, approx)

    def c_remove_from_collection(self, frame, *a):
        loc = self.loc(frame)
        opts, pos = self.parse_args("remove_from_collection", a, {"-intersect"}, {}, (), loc)
        if len(pos) < 2:
            return pos[0] if pos else ""
        keys, approx = self._keys_of(pos[0])
        rem, ap2 = self._keys_of(pos[1])
        if not rem and not ap2:
            types = tuple(sorted(set(k[0] for k in keys))) or ("port", "cell", "pin", "net", "clock")
            rem, ap2 = self.resolve(pos[1], types, loc, "remove_from_collection")
        rs = set(rem)
        if "-intersect" in opts:
            keys = [k for k in keys if k in rs]
        else:
            keys = [k for k in keys if k not in rs]
        return self.new_coll(keys, approx or ap2)

    def c_filter_collection(self, frame, *a):
        loc = self.loc(frame)
        opts, pos = self.parse_args("filter_collection", a, {"-regexp", "-nocase"}, {}, (), loc)
        if len(pos) < 2:
            return pos[0] if pos else ""
        keys, approx = self._keys_of(pos[0])
        keys = self._filter(keys, pos[1], loc, "-regexp" in opts, "-nocase" in opts)
        return self.new_coll(keys, approx)

    def c_index_collection(self, frame, *a):
        if len(a) < 2:
            return ""
        keys, approx = self._keys_of(a[0])
        try:
            i = int(a[1])
            j = int(a[2]) if len(a) > 2 else i
        except ValueError:
            return ""
        return self.new_coll(keys[i:j + 1], approx)

    def c_get_attribute(self, frame, *a):
        loc = self.loc(frame)
        opts, pos = self.parse_args("get_attribute", a, {"-quiet", "-value_list"},
                                    {"-class": "str"}, (), loc)
        if len(pos) < 2:
            self.add("PARSE-003", "get_attribute needs <objects> <attribute>", loc)
            return ""
        keys, _ = self._keys_of(pos[0])
        if not keys and "-class" in opts:
            keys = self.implicit(pos[0], (opts["-class"],)) or []
        vals = []
        for k in keys:
            try:
                v = self.attr(k, pos[1])
            except UnsupportedAttr:
                self._unsupported(pos[1], loc)
                return ""
            if isinstance(v, bool):
                v = "true" if v else "false"
            vals.append("" if v is None else (("%g" % v) if isinstance(v, float) else str(v)))
        if len(vals) == 1:
            return vals[0]
        return tuple(vals)

    # ------------------------------------------------------- misc commands
    def c_noop(self, name, frame, a):
        loc = self.loc(frame)
        n = self._noop_seen.get(name, 0)
        self._noop_seen[name] = n + 1
        if n == 0:
            self.add("PARSE-005", "%s accepted but not modelled (ignored by sdc_qc)" % name, loc)
        return ""

    def c_unknown(self, frame, *a):
        loc = self.loc(frame)
        name = a[0] if a else ""
        hint = ""
        if re.match(r"^[\d:*]+$", name):
            hint = (" (an unbraced bus index like x[%s] is Tcl command substitution; "
                    "write {x[%s]} or x\\[%s\\])" % (name, name, name))
        self.add("PARSE-002", "unknown command '%s'%s" % (name, hint), loc, obj=name)
        return ""

    def c_source(self, frame, lvl, *a):
        loc = self.loc(frame)
        opts, pos = self.parse_args("source", a, {"-echo", "-verbose", "-continue_on_error"},
                                    {"-encoding": "str"}, (), loc)
        if not pos:
            self.add("PARSE-003", "source: no file given", loc)
            return ""
        path = self.find_file(pos[-1])
        if path is None:
            self.add("PARSE-004", "sourced file not found: %s" % pos[-1], loc, obj=pos[-1])
            return ""
        self.run_file(path, int(lvl))
        return ""

    def find_file(self, name):
        cands = [name]
        if not os.path.isabs(name):
            if self.file_stack:
                cands.append(os.path.join(os.path.dirname(self.file_stack[-1][0]), name))
            for sp in self.opts.search_path:
                cands.append(os.path.join(sp, name))
        for c in cands:
            if os.path.isfile(c):
                return c
        return None

    def run_file(self, path, lvl=0):
        with open(path, "rb") as f:
            text = f.read().decode("latin-1")
        lines = text.split("\n")
        self.m.files.append(path)
        frame = [path, lines, 1]
        self.file_stack.append(frame)
        t = self.tcl
        try:
            buf, start = [], 1
            for ln, line in enumerate(lines, 1):
                if not buf:
                    s = line.strip()
                    if not s or (s[0] == "#" and not s.endswith("\\")):
                        continue
                    start = ln
                buf.append(line)
                chunk = "\n".join(buf)
                if line.endswith("\\") or not t.call("info", "complete", chunk + "\n"):
                    continue
                buf = []
                frame[2] = start
                res = t.splitlist(self.sdc_eval(t.call("list", "__qc_run", lvl, chunk)))
                code = int(res[0])
                if code == 1:
                    msg = str(res[1])
                    self.add("PARSE-001", "Tcl error: %s" % msg.split("\n")[0][:300],
                             Loc(path, start, " ".join(chunk.split())[:240]))
                elif code == 2:
                    break
                elif code in (3, 4):
                    self.add("PARSE-001", "break/continue outside a loop",
                             Loc(path, start, " ".join(chunk.split())[:240]))
            if buf:
                self.add("PARSE-001", "incomplete command at end of file (unbalanced braces/"
                         "quotes?)", Loc(path, start, " ".join("\n".join(buf).split())[:240]))
        finally:
            self.file_stack.pop()

    # ------------------------------------------------------ constraint cmds
    def c_constraint(self, name, frame, a):
        loc = self.loc(frame)
        spec = SDC_CMDS[name]
        opts, pos = self.parse_args(name, a, spec["flags"], spec["vals"], spec["multi"], loc)
        nval, otypes, oreq = spec["pos"]
        values = pos[:nval]
        if len(values) < nval:
            self.add("PARSE-003", "%s: missing value argument" % name, loc)
            return ""
        rest = pos[nval:]
        bad = []
        if nval and name not in _NON_NUMERIC_VALUE and name not in ("set_input_delay",
                                                                     "set_output_delay"):
            bad.extend(v for v in values if _num(v) is None)
        bad.extend(v for o, v in opts.items()
                   if spec["vals"].get(o) == "num" and o != "-period" and _num(v) is None)
        for v in bad:
            self.add("PARSE-003", "%s: '%s' is not a finite number" % (name, v), loc)
        rec = {"cmd": name, "opts": opts, "values": values, "loc": loc, "objs": [],
               "approx": False, "dropped": False}
        # resolve object-valued options
        for o, v in list(opts.items()):
            vt = spec["vals"].get(o)
            if isinstance(vt, tuple) and vt[0] == "obj":
                vlist = v if isinstance(v, list) else [v]
                res = []
                for vv in vlist:
                    keys, approx = self.resolve(vv, vt[1], loc, "%s %s" % (name, o))
                    rec["approx"] = rec["approx"] or approx
                    if not keys and not approx:
                        rec["dropped"] = True
                        self.add("OBJ-002", "%s: %s resolved to no objects; the STA tool will "
                                 "reject or ignore this constraint" % (name, o), loc)
                    res.append(keys)
                opts[o] = res if isinstance(v, list) else res[0]
        if otypes:
            keys, approx = [], False
            for r in rest:
                k, ap = self.resolve(r, tuple(otypes.split(",")), loc, "%s objects" % name)
                keys.extend(k)
                approx = approx or ap
            rec["objs"], rec["approx"] = keys, rec["approx"] or approx
            if rest and not keys and not approx:
                rec["dropped"] = True
                self.add("OBJ-002", "%s: object list resolved to nothing; constraint dropped"
                         % name, loc)
            elif oreq and not rest:
                self.add("PARSE-003", "%s: object list missing" % name, loc)
                rec["dropped"] = True
        elif rest:
            self.add("PARSE-003", "%s: unexpected argument(s) %s" % (name, " ".join(rest)[:80]), loc)
        if name not in _NOT_CLOCK_REFS:
            for v in list(opts.values()) + [rec["objs"]]:
                for grp in (v if isinstance(v, list) else []):
                    for k in (grp if isinstance(grp, list) else [grp]):
                        if isinstance(k, tuple) and k[0] == "clock":
                            self.m.clock_refs.add(k[1])
        handler = getattr(self, "r_" + name, None)
        if handler is not None:
            return handler(rec) or ""
        self.m.records.append(rec)
        return ""

    def r_create_clock(self, rec):
        o, loc = rec["opts"], rec["loc"]
        name = o.get("-name")
        if name is None:
            if not rec["objs"]:
                if not rec["dropped"]:
                    self.add("CLK-003", "create_clock without sources needs -name (virtual clock)",
                             loc)
                return
            name = self.obj_name(rec["objs"][0])
        period = _num(o.get("-period"))
        if "-period" not in o:
            self.add("CLK-003", "create_clock %s: -period missing" % name, loc)
            period = None
        elif period is None or period <= 0:
            self.add("CLK-003", "create_clock %s: invalid period '%s'" % (name, o.get("-period")),
                     loc)
            period = None
        wf = [0.0, period / 2.0] if period else []
        if "-waveform" in o:
            wv = [_num(x) for x in self.tcl.splitlist(o["-waveform"])]
            if any(x is None for x in wv) or len(wv) < 2 or len(wv) % 2:
                self.add("CLK-004", "create_clock %s: waveform must be an even number of edges"
                         % name, loc)
            else:
                if any(b < a for a, b in zip(wv, wv[1:])):
                    self.add("CLK-004", "create_clock %s: waveform edges not increasing" % name, loc)
                if period and (wv[-1] - wv[0] > period + 1e-9):
                    self.add("CLK-004", "create_clock %s: waveform spans more than one period"
                             % name, loc)
                wf = wv
        self._define_clock(Clock(name, period, wf, list(rec["objs"]), "-add" in o, None, loc),
                           rec)

    def _define_clock(self, ck, rec):
        m, loc = self.m, ck.loc
        if rec["dropped"] and not ck.sources:
            return
        if ck.name in m.clocks:
            prev = m.clocks[ck.name]
            self.add("CLK-001", "clock %s redefined (previous definition %s:%d is replaced)"
                     % (ck.name, prev.loc.file, prev.loc.line), loc, obj=ck.name)
            del m.clocks[ck.name]
        if not ck.add and ck.sources:
            srcs = set(ck.sources)
            for other in list(m.clocks.values()):
                if other.sources and srcs.intersection(other.sources):
                    self.add("CLK-002", "clock %s defined on the source of clock %s without -add; "
                             "%s is removed from that source" % (ck.name, other.name, other.name),
                             loc, obj=ck.name)
                    other.sources = [s for s in other.sources if s not in srcs]
                    if not other.sources:
                        del m.clocks[other.name]
        for s in ck.sources:
            if s[0] == "port" and self.d.ports[s[1]].dir == "out":
                self.add("CLK-005", "clock %s defined on output port %s" % (ck.name, self.obj_name(s)),
                         loc, obj=self.obj_name(s))
            elif s[0] == "pin" and self.d.refs[self.d.cell_ref[s[1]]].kind == REF_MODULE:
                self.add("CLK-005", "clock %s defined on hierarchical pin %s (tools may move it; "
                         "prefer a leaf pin or port)" % (ck.name, self.obj_name(s)), loc,
                         obj=self.obj_name(s), sev=SEV_INFO)
        m.clocks[ck.name] = ck
        m.clock_defs.append(ck)
        self.tracer = None

    def r_create_generated_clock(self, rec):
        o, loc = rec["opts"], rec["loc"]
        name = o.get("-name") or (self.obj_name(rec["objs"][0]) if rec["objs"] else None)
        if name is None:
            if not rec["dropped"]:
                self.add("CLK-009", "create_generated_clock without source objects", loc)
            return
        gen = {"source": o.get("-source"), "master": None}
        if "-source" not in o:
            self.add("CLK-006", "generated clock %s: -source missing" % name, loc, obj=name)
        elif not o["-source"]:
            self.add("CLK-006", "generated clock %s: -source did not resolve" % name, loc, obj=name)
        if "-master_clock" in o:
            mk = o["-master_clock"]
            if not mk:
                self.add("CLK-007", "generated clock %s: -master_clock not defined" % name, loc,
                         obj=name)
            else:
                gen["master"] = mk[0][1]
        srcs = set(o.get("-source") or [])
        at_source = [c.name for c in self.m.clocks.values() if srcs.intersection(c.sources)]
        if srcs:
            if gen["master"] and at_source and gen["master"] not in at_source:
                self.add("CLK-008", "generated clock %s: -master_clock %s is not defined on "
                         "-source (clocks there: %s)" % (name, gen["master"], ", ".join(at_source)),
                         loc, obj=name)
            elif not gen["master"] and len(at_source) > 1:
                self.add("CLK-008", "generated clock %s: several clocks on -source (%s) and no "
                         "-master_clock" % (name, ", ".join(at_source)), loc, obj=name)
            elif not at_source:
                self.add("CLK-008", "generated clock %s: no clock is defined directly on -source; "
                         "reachability of the master through the netlist is not checked "
                         "(deferred)" % name, loc, obj=name, sev=SEV_INFO)
        # value checks
        div, mul = _num(o.get("-divide_by")), _num(o.get("-multiply_by"))
        kinds = [k for k in ("-divide_by", "-multiply_by", "-edges") if k in o]
        if len(kinds) > 1:
            self.add("CLK-009", "generated clock %s: %s are mutually exclusive"
                     % (name, " and ".join(kinds)), loc, obj=name)
        if not kinds and "-combinational" not in o:
            self.add("CLK-009", "generated clock %s: none of -divide_by/-multiply_by/-edges/"
                     "-combinational given" % name, loc, obj=name, sev=SEV_WARNING)
        for k, v in (("-divide_by", div), ("-multiply_by", mul)):
            if k in o and (v is None or v < 1 or v != int(v)):
                self.add("CLK-009", "generated clock %s: %s must be a positive integer (got %s)"
                         % (name, k, o[k]), loc, obj=name)
        if "-edges" in o:
            ev = [_num(x) for x in self.tcl.splitlist(o["-edges"])]
            if any(x is None for x in ev) or len(ev) < 3 or len(ev) % 2 == 0 or \
                    any(b < a for a, b in zip(ev, ev[1:])):
                self.add("CLK-009", "generated clock %s: -edges needs an odd count (>=3) of "
                         "non-decreasing edge numbers" % name, loc, obj=name)
            if "-edge_shift" in o and len(self.tcl.splitlist(o["-edge_shift"])) != len(ev):
                self.add("CLK-009", "generated clock %s: -edge_shift count differs from -edges"
                         % name, loc, obj=name)
        # derive period when the master is known
        period = None
        mname = gen["master"] or (at_source[0] if len(at_source) == 1 else None)
        mck = self.m.clocks.get(mname) if mname else None
        if mck is not None and mck.period:
            if div:
                period = mck.period * div
            elif mul:
                period = mck.period / mul
            elif "-edges" in o:
                ev = [_num(x) for x in self.tcl.splitlist(o["-edges"])]
                if len(ev) >= 3 and all(x is not None for x in ev):
                    period = (ev[2] - ev[0]) * mck.period / 2.0
            else:
                period = mck.period
        gen["master"] = mname
        self._define_clock(Clock(name, period, [], list(rec["objs"]), "-add" in o, gen, loc), rec)

    def r_set_clock_groups(self, rec):
        o, loc = rec["opts"], rec["loc"]
        groups = [set(k[1] for k in g) for g in o.get("-group", [])]
        if not groups:
            self.add("PARSE-003", "set_clock_groups without -group", loc)
            return
        seen = {}
        for gi, g in enumerate(groups):
            for c in g:
                if c in seen and seen[c] != gi:
                    self.add("CG-001", "clock %s appears in more than one -group" % c, loc, obj=c)
                seen[c] = gi
        if len(groups) == 1:
            self.add("CG-002", "single -group: its clocks are exclusive with ALL other clocks "
                     "(including clocks defined later)", loc)
        kind = "asynchronous" if "-asynchronous" in o else (
            "physically_exclusive" if "-physically_exclusive" in o else "logically_exclusive")
        self.m.clock_groups.append({"kind": kind, "groups": groups, "loc": loc})

    def _io(self, rec, kind):
        o, loc = rec["opts"], rec["loc"]
        val = _num(rec["values"][0])
        if val is None:
            self.add("PARSE-003", "%s: delay value '%s' is not a number"
                     % (rec["cmd"], rec["values"][0]), loc)
        ck = o.get("-clock")
        cname = ck[0][1] if ck else None
        if "-clock" not in o:
            self.add("IO-004", "%s without -clock (delay not related to any clock)" % rec["cmd"], loc)
        if rec["dropped"]:
            return          # OBJ-002 already reported; must not count as applied
        pins = [self.obj_name(k) for k in rec["objs"] if k[0] == "pin"]
        if pins:
            self.add("IO-009", "%s on %d internal pin(s): legal in PrimeTime, but not modelled "
                     "by sdc_qc (no port coverage credit)" % (rec["cmd"], len(pins)), loc,
                     obj=" ".join(pins[:self.opts.max_examples]))
        ports, bad = [], []
        for k in rec["objs"]:
            if k[0] == "port":
                p = self.d.ports[k[1]]
                if (kind == "in" and p.dir == "out") or (kind == "out" and p.dir == "in"):
                    bad.append(p.name)
                else:
                    ports.append(k[1])
        if bad:
            self.add("IO-003", "%s on %d %s port(s)" % (rec["cmd"], len(bad),
                     "output" if kind == "in" else "input"), loc,
                     obj=" ".join(compress_names(bad, self.opts.max_examples)))
        mm = "max" if "-max" in o and "-min" not in o else ("min" if "-min" in o and "-max" not in o
                                                            else "both")
        self.m.io.append({"kind": kind, "cmd": rec["cmd"], "value": val, "clock": cname,
                          "ports": ports, "minmax": mm, "loc": loc,
                          "rf": "-rise" in o or "-fall" in o, "clock_fall": "-clock_fall" in o})

    def r_set_input_delay(self, rec):
        self._io(rec, "in")

    def r_set_output_delay(self, rec):
        self._io(rec, "out")

    def _exc(self, rec, kind):
        o = rec["opts"]
        frm = [k for opt in ("-from", "-rise_from", "-fall_from") for k in o.get(opt, [])]
        to = [k for opt in ("-to", "-rise_to", "-fall_to") for k in o.get(opt, [])]
        thr = [g for opt in _EXC_MULTI for g in o.get(opt, [])]
        rec.update(kind=kind, frm=frm, to=to, thr=thr)
        self.m.exceptions.append(rec)

    def r_set_false_path(self, rec):
        self._exc(rec, "false")

    def r_set_multicycle_path(self, rec):
        self._exc(rec, "mcp")

    def r_set_max_delay(self, rec):
        self._exc(rec, "max")

    def r_set_min_delay(self, rec):
        self._exc(rec, "min")

    def r_set_case_analysis(self, rec):
        v = rec["values"][0].lower()
        norm = {"0": "0", "zero": "0", "1": "1", "one": "1", "rise": "rise", "rising": "rise",
                "fall": "fall", "falling": "fall"}.get(v)
        if norm is None:
            self.add("CASE-001", "set_case_analysis value '%s' is not 0/1/zero/one/rise/rising/"
                     "fall/falling" % rec["values"][0], rec["loc"])
            return
        for k in rec["objs"]:
            prev = self.m.case.get(k)
            if prev and prev[0] != norm:
                self.add("CASE-002", "set_case_analysis %s on %s overrides %s from %s:%d"
                         % (norm, self.obj_name(k), prev[0], prev[1].file, prev[1].line),
                         rec["loc"], obj=self.obj_name(k))
            self.m.case[k] = (norm, rec["loc"])

    def _drive(self, rec):
        bad = []
        for k in rec["objs"]:
            if k[0] == "port":
                if self.d.ports[k[1]].dir == "out":
                    bad.append(self.obj_name(k))
                self.m.drive_ports.add(k[1])
        if bad:
            self.add("IO-003", "%s on %d output port(s)" % (rec["cmd"], len(bad)), rec["loc"],
                     obj=" ".join(compress_names(bad, self.opts.max_examples)), sev=SEV_WARNING)

    r_set_driving_cell = _drive
    r_set_drive = _drive
    r_set_input_transition = _drive

    def r_set_load(self, rec):
        for k in rec["objs"]:
            if k[0] == "port":
                self.m.load_ports.add(k[1])

    def r_set_units(self, rec):
        for k, v in rec["opts"].items():
            self.m.units[k.lstrip("-")] = (v, rec["loc"])

    def r_set_propagated_clock(self, rec):
        self.m.propagated.append(rec["loc"])

    def r_set_disable_timing(self, rec):
        o = rec["opts"]
        for side in ("-from", "-to"):
            if side not in o:
                continue
            pn = o[side]
            for k in rec["objs"]:
                if k[0] == "cell":
                    r = self.d.refs[self.d.cell_ref[k[1]]]
                    if r.kind == REF_LIB and pn not in r.pidx and pn not in r.bus:
                        self.add("MISC-001", "set_disable_timing %s %s: no such pin on %s (%s)"
                                 % (side, pn, self.obj_name(k), r.name), rec["loc"])
        self.m.disable.append(rec)

    def r_set_hierarchy_separator(self, rec):
        if rec["values"][0] != "/":
            self.add("MISC-002", "hierarchy separator '%s' is not supported (only '/')"
                     % rec["values"][0], rec["loc"])

    def get_tracer(self):
        if self.tracer is None:
            self.tracer = ClockTracer(self.d, self.m)
        return self.tracer

    # ------------------------------------------------------------ run mode
    def run(self, steps):
        t0 = time.time()
        for kind, a, b in steps:
            if kind == "var":
                self.sdc_eval(self.tcl.call("list", "set", "::" + a, b))
            else:
                p = self.find_file(a)
                if p is None:
                    self.add("PARSE-004", "mode SDC file not found: %s" % a, Loc(a, 0, ""), obj=a)
                    continue
                self.run_file(p, 0)
        # aggregate unknown/noop counts
        self.m.seconds = time.time() - t0
        return self.m


# =============================================================================
# 7. Clock tracing (structural, backward) and per-mode checks
#
# From each register/macro clock pin, walk backwards: net -> driver. Pass
# through buffers, inverters (Liberty function) and ICGs (clock pin) until a
# clock source (port/pin with create_clock/create_generated_clock) is met.
# Anything else stops the walk and is reported by its root. Results are
# memoized per net, so shared clock-tree nets are walked once.
# =============================================================================

class ClockTracer(object):
    def __init__(self, d, m):
        self.d = d
        self.memo = {}
        self.slot_src = collections.defaultdict(set)    # leaf pin slot -> clocks
        self.net_src = collections.defaultdict(set)     # net -> clocks (ports, hier/out pins)
        for ck in m.clocks.values():
            for s in ck.sources:
                if s[0] == "port":
                    n = d.ports[s[1]].net
                    if n >= 0:
                        self.net_src[d.find(n)].add(ck.name)
                elif s[0] == "pin":
                    c, p = s[1], s[2]
                    r = d.refs[d.cell_ref[c]]
                    slot = d.cell_off[c] + p
                    if p < d.ncell_pins(c):
                        self.slot_src[slot].add(ck.name)
                        n = d.conn[slot]
                        if n >= 0 and (r.kind == REF_MODULE or r.pdir[p] in ("out", "inout")):
                            self.net_src[d.find(n)].add(ck.name)
        self.const = set(d.find(k) for k in d.const_nets)

    def trace_slot(self, slot):
        d = self.d
        if slot in self.slot_src:
            return ("clk", frozenset(self.slot_src[slot]))
        n = d.conn[slot]
        if n == UNCONN:
            c = d.slot_cell(slot)
            return ("undriven", "clock pin unconnected (e.g. %s)" % d.pin_name(c, slot - d.cell_off[c]))
        if n in (CONST0, CONST1):
            return ("const", "clock pin tied to constant %d" % (0 if n == CONST0 else 1))
        return self.trace_net(d.find(n))

    def trace_net(self, n):
        d = self.d
        memo = self.memo
        drv, off, cref, refs = d.driver, d.cell_off, d.cell_ref, d.refs
        path, seen = [], set()
        while True:
            r0 = memo.get(n)
            if r0 is not None:
                res = r0
                break
            if n in seen:
                res = ("unres", "combinational loop in clock path at net %s" % d.net_names[n])
                break
            seen.add(n)
            path.append(n)
            if n in self.net_src:
                res = ("clk", frozenset(self.net_src[n]))
                break
            if n in self.const:
                res = ("const", "clock net %s tied constant (assign)" % d.net_names[n])
                break
            dv = drv[n]
            if dv == -1:
                res = ("undriven", "net %s has no driver" % d.net_names[n])
                break
            if dv <= -2:
                res = ("noclk", "input port %s has no clock defined" % d.ports[-(dv + 2)].name)
                break
            if dv in self.slot_src:
                res = ("clk", frozenset(self.slot_src[dv]))
                break
            c = bisect.bisect_right(off, dv) - 1
            r = refs[cref[c]]
            ip = r.buf_in if r.buf_in >= 0 else r.icg_in
            if ip >= 0:
                s2 = off[c] + ip
                if s2 in self.slot_src:
                    res = ("clk", frozenset(self.slot_src[s2]))
                    break
                n2 = d.conn[s2]
                if n2 < 0:
                    res = ("const" if n2 in (CONST0, CONST1) else "undriven",
                           "input of %s (%s) is %s" % (d.cell_names[c], r.name,
                                                      "constant" if n2 in (CONST0, CONST1)
                                                      else "unconnected"))
                    break
                n = d.find(n2)
                continue
            pname = r.pins[dv - off[c]]
            if r.seq:
                res = ("noclk", "output %s/%s of sequential cell %s (divider? no "
                       "create_generated_clock there)" % (d.cell_names[c], pname, r.name))
            elif r.kind in (REF_BLACKBOX, REF_UNRESOLVED):
                res = ("unres", "black box %s (%s)" % (d.cell_names[c], r.name))
            else:
                res = ("unres", "%s/%s (%s)" % (d.cell_names[c], pname, r.name))
            break
        for x in path:
            memo[x] = res
        return res


def register_clock_slots(d):
    """All (slot) of clock pins of sequential / macro Liberty cells (mode independent)."""
    out = array.array("q")
    cref, off = d.cell_ref, d.cell_off
    want = {}
    for ri, r in enumerate(d.refs):
        if r.kind == REF_LIB and r.clock_pins and (r.seq or r.icg_in >= 0):
            want[ri] = sorted(r.clock_pins)
    for c in range(len(d.cell_names)):
        ps = want.get(cref[c])
        if ps:
            base = off[c]
            lim = off[c + 1] - base
            for p in ps:
                if p < lim:
                    out.append(base + p)
    return out


_TIME_UNITS = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12, "fs": 1e-15}
_CAP_UNITS = {"f": 1.0, "mf": 1e-3, "uf": 1e-6, "nf": 1e-9, "pf": 1e-12, "ff": 1e-15}


def _unit_value(s, table):
    if not s:
        return None
    s = s.strip().lower().replace(" ", "").replace(",", "")
    m = re.match(r"^([\d.eE+-]*)([a-z]+)$", s)
    if not m or m.group(2) not in table:
        return None
    mult = float(m.group(1)) if m.group(1) else 1.0
    return mult * table[m.group(2)]


def _cap_unit_value(s):
    # Liberty capacitive_load_unit is "1,ff" (after comma removal "1ff")
    return _unit_value(s, _CAP_UNITS)


def compress_names(names, limit=None):
    """['d[0]','d[1]','d[2]','x'] -> ['d[2:0]', 'x'] (only full contiguous runs)."""
    groups = collections.OrderedDict()
    singles = []
    for n in names:
        bm = _LIB_BITSEL.match(n)
        if bm and bm.group(3) is None:
            groups.setdefault(bm.group(1), []).append(int(bm.group(2)))
        else:
            singles.append(n)
    out = list(singles)
    for base, idx in groups.items():
        idx = sorted(set(idx))
        start = prev = idx[0]
        for i in idx[1:] + [None]:
            if i is not None and i == prev + 1:
                prev = i
                continue
            out.append("%s[%d]" % (base, start) if start == prev else "%s[%d:%d]" % (base, prev, start))
            if i is not None:
                start = prev = i
    if limit and len(out) > limit:
        return out[:limit] + ["... (+%d more)" % (len(out) - limit)]
    return out


class ModeChecker(object):
    def __init__(self, eng, reg_slots, opts):
        self.eng, self.d, self.m = eng, eng.d, eng.m
        self.reg_slots, self.opts = reg_slots, opts

    def add(self, *a, **k):
        self.eng.add(*a, **k)

    def run(self):
        self.check_units()
        self.check_clocks()
        if not self.opts.no_clock_trace:
            self.check_coverage()
        self.check_io()
        self.check_exceptions()

    # -------------------------------------------------------------- units
    def check_units(self):
        d, m = self.d, self.m
        lib_t = _unit_value(d.time_unit, _TIME_UNITS) if d.time_unit else None
        lib_c = _cap_unit_value(d.cap_unit) if d.cap_unit else None
        sdc_t = sdc_c = None
        if "time" in m.units:
            v, loc = m.units["time"]
            sdc_t = _unit_value(v, _TIME_UNITS)
            if sdc_t is None:
                self.add("PARSE-003", "set_units -time '%s' not understood" % v, loc)
            elif lib_t and abs(sdc_t / lib_t - 1) > 1e-6:
                self.add("UNIT-001", "set_units -time %s but Liberty time_unit is %s"
                         % (v, d.time_unit), loc)
        if "capacitance" in m.units:
            v, loc = m.units["capacitance"]
            sdc_c = _unit_value(v, _CAP_UNITS)
            if sdc_c is None:
                self.add("PARSE-003", "set_units -capacitance '%s' not understood" % v, loc)
            elif lib_c and abs(sdc_c / lib_c - 1) > 1e-6:
                self.add("UNIT-001", "set_units -capacitance %s but Liberty capacitive_load_unit "
                         "is %s" % (v, d.cap_unit), loc)
        unit = sdc_t or lib_t or 1e-9
        real = [c for c in m.clocks.values() if c.period and not c.virtual and c.gen is None]
        if real:
            ns = [(c, c.period * unit / 1e-9) for c in real]
            fast = [(c, p) for c, p in ns if p < 0.02]
            for c, p in fast:
                self.add("UNIT-003", "clock %s period %g = %.4g ns (> 50 GHz) in time unit %s"
                         % (c.name, c.period, p, "%gs" % unit), c.loc, obj=c.name)
            if all(p > 100 for c, p in ns):
                self.add("UNIT-003", "all clocks are slower than 10 MHz (periods %s in unit %gs); "
                         "is the SDC written in ps while the library unit is ns? (heuristic)"
                         % (", ".join("%g" % c.period for c, p in ns[:5]), unit), real[0].loc)

    # ------------------------------------------------------------- clocks
    def check_clocks(self):
        m = self.m
        if not m.clocks:
            self.add("CLK-011", "mode defines no clocks", Loc(m.files[0] if m.files else "", 0, ""))
        for c in m.clocks.values():
            if c.virtual and c.name not in m.clock_refs:
                self.add("CLK-010", "virtual clock %s is never referenced" % c.name, c.loc,
                         obj=c.name)
        for loc in m.propagated[:1]:
            self.add("CLK-012", "set_propagated_clock used; pre-CTS the clocks are normally ideal "
                     "and APR/CTS decides propagation", loc)

    # ----------------------------------------------------------- coverage
    def check_coverage(self):
        d = self.d
        tr = self.eng.get_tracer()
        groups = collections.defaultdict(list)
        nclk = 0
        per_clock = collections.Counter()
        for slot in self.reg_slots:
            res = tr.trace_slot(slot)
            if res[0] == "clk":
                nclk += 1
                for c in res[1]:
                    per_clock[c] += 1
            else:
                groups[res].append(slot)
        self.m.coverage = {"clocked": nclk, "total": len(self.reg_slots),
                           "per_clock": dict(per_clock)}
        mx = self.opts.max_examples
        loc = Loc(self.m.files[0] if self.m.files else "", 0, "")
        for (kind, root), slots in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            ex = []
            for s in slots[:mx]:
                c = d.slot_cell(s)
                ex.append(d.pin_name(c, s - d.cell_off[c]))
            rule = {"noclk": "COV-001", "unres": "COV-002", "undriven": "COV-003",
                    "const": "COV-003"}[kind]
            self.add(rule, "%d clock pin(s): %s" % (len(slots), root), loc,
                     obj=" ".join(ex) + (" ..." if len(slots) > mx else ""))

    # ----------------------------------------------------------------- IO
    def check_io(self):
        d, m, mx = self.d, self.m, self.opts.max_examples
        clock_ports = set()
        for c in m.clocks.values():
            for s in c.sources:
                if s[0] == "port":
                    clock_ports.add(s[1])
        case_ports = set(k[1] for k in m.case if k[0] == "port")
        fp_from = set()
        for e in m.exceptions:
            if e["kind"] == "false" and not e["to"] and not e["thr"]:
                fp_from.update(k[1] for k in e["frm"] if k[0] == "port")
        has = {"in": set(), "out": set()}
        minmax = collections.defaultdict(set)
        for r in m.io:
            has[r["kind"]].update(r["ports"])
            for p in r["ports"]:
                minmax[(r["kind"], p, r["clock"], r["clock_fall"])].add(r["minmax"])
            ck = m.clocks.get(r["clock"]) if r["clock"] else None
            if ck is not None and ck.period and r["value"] is not None and r["minmax"] != "min":
                frac = r["value"] / ck.period
                if frac >= self.opts.io_threshold:
                    self.add("IO-005", "%s %g is %.0f%% of clock %s period %g"
                             % (r["cmd"], r["value"], frac * 100, ck.name, ck.period), r["loc"],
                             obj=" ".join(compress_names([d.ports[p].name for p in r["ports"]], mx)))
        mm_bad = collections.defaultdict(list)
        for (kind, p, ck, cf), s in minmax.items():
            if "both" not in s and len(s) == 1:
                mm_bad[(kind, next(iter(s)))].append(d.ports[p].name)
        for (kind, only), ports in mm_bad.items():
            self.add("IO-006", "%d %s port(s) have only a -%s delay (the -%s value is not set; "
                     "check how your STA tool defaults it)"
                     % (len(ports), "input" if kind == "in" else "output", only,
                        "min" if only == "max" else "max"),
                     Loc(m.files[0] if m.files else "", 0, ""),
                     obj=" ".join(compress_names(ports, mx)))
        loc0 = Loc(m.files[0] if m.files else "", 0, "")
        miss_in = [p.name for i, p in enumerate(d.ports) if p.dir in ("in", "inout")
                   and i not in has["in"] and i not in clock_ports and i not in case_ports
                   and i not in fp_from]
        miss_out = [p.name for i, p in enumerate(d.ports) if p.dir in ("out", "inout")
                    and i not in has["out"] and i not in case_ports]
        if miss_in:
            self.add("IO-001", "%d input port(s) without set_input_delay (excluding clock sources, "
                     "case-analysis and false-path -from ports)" % len(miss_in), loc0,
                     obj=" ".join(compress_names(miss_in)))
        if miss_out:
            self.add("IO-002", "%d output port(s) without set_output_delay" % len(miss_out), loc0,
                     obj=" ".join(compress_names(miss_out)))
        nodrv = [p.name for i, p in enumerate(d.ports) if p.dir in ("in", "inout")
                 and i not in m.drive_ports]
        noload = [p.name for i, p in enumerate(d.ports) if p.dir in ("out", "inout")
                  and i not in m.load_ports]
        if nodrv:
            self.add("IO-007", "%d input port(s) without set_driving_cell/set_drive/"
                     "set_input_transition" % len(nodrv), loc0, obj=" ".join(compress_names(nodrv)))
        if noload:
            self.add("IO-008", "%d output port(s) without set_load" % len(noload), loc0,
                     obj=" ".join(compress_names(noload)))
        self.m.io_cov = {"in": has["in"], "out": has["out"]}

    # --------------------------------------------------------- exceptions
    def _valid(self, k, side):
        """True / False / None(unknown) for a -from (side='from') or -to object."""
        d = self.d
        t = k[0]
        if t == "clock":
            return True
        if t == "port":
            dr = d.ports[k[1]].dir
            return dr in (("in", "inout") if side == "from" else ("out", "inout"))
        if t == "cell":
            r = d.refs[d.cell_ref[k[1]]]
            if r.kind in (REF_BLACKBOX, REF_UNRESOLVED):
                return None
            return bool(r.seq) and r.kind == REF_LIB
        if t == "pin":
            r = d.refs[d.cell_ref[k[1]]]
            if r.kind in (REF_BLACKBOX, REF_UNRESOLVED) or r.pins[k[2]] in r.unknown_pins:
                return None
            if r.kind == REF_MODULE:
                return False
            if side == "from":
                return k[2] in r.clock_pins and bool(r.seq)
            return k[2] in r.endpoint_pins
        return False

    def check_exceptions(self):
        m, mx = self.m, self.opts.max_examples
        eng = self.eng
        by_key = {}
        hold_keys = set()
        setup_mcp = []
        for e in m.exceptions:
            if e["dropped"]:
                continue
            o, loc = e["opts"], e["loc"]
            if not e["frm"] and not e["to"] and not e["thr"]:
                self.add("EXC-006", "%s without -from/-to/-through applies to every path"
                         % e["cmd"], loc)
            seg = e["kind"] in ("max", "min")
            for side, keys, rule in (("from", e["frm"], "EXC-001"), ("to", e["to"], "EXC-002")):
                bad = [k for k in keys if self._valid(k, side) is False]
                if bad:
                    names = [eng.obj_name(k) for k in bad]
                    if seg:
                        msg = ("%d -%s object(s) are not timing %spoints; %s will segment the "
                               "path there (check it is intended)" % (len(bad), side,
                                                                      "start" if side == "from" else "end",
                                                                      e["cmd"]))
                        self.add(rule, msg, loc, obj=" ".join(names[:mx]), sev=SEV_INFO)
                    else:
                        msg = ("%d of %d -%s object(s) are not valid timing %spoints (e.g. "
                               "hierarchical/combinational pins, Q pins); STA tools ignore "
                               "them (PrimeTime: see report_exceptions -ignored)%s" % (len(bad), len(keys), side,
                                           "start" if side == "from" else "end",
                                           " -> this exception is ignored entirely" if len(bad) == len(keys) else ""))
                        self.add(rule, msg, loc, obj=" ".join(names[:mx]) +
                                 (" ..." if len(names) > mx else ""))
            key = (frozenset(e["frm"]), tuple(frozenset(t) for t in e["thr"]), frozenset(e["to"]),
                   bool(o.get("-rise")), bool(o.get("-fall")))
            hs = "hold" if "-hold" in o else "setup"
            if e["kind"] == "mcp":
                if hs == "hold":
                    hold_keys.add(key)
                else:
                    try:
                        mult = float(e["values"][0])
                    except ValueError:
                        mult = 1
                    if mult > 1:
                        setup_mcp.append((key, e, mult))
            full = (key, e["kind"], hs if e["kind"] in ("mcp", "false") else "")
            prev = by_key.get(full)
            if prev is not None:
                same = prev["values"] == e["values"]
                self.add("EXC-004", "%s %s %s:%d (%s)" % (e["cmd"], "duplicates" if same else
                         "overrides", prev["loc"].file, prev["loc"].line, prev["cmd"]), loc)
            else:
                by_key[full] = e
            if e["kind"] != "false":
                fk = (key, "false", hs if e["kind"] == "mcp" else "setup")
                if fk in by_key or (key, "false", "") in by_key:
                    fp = by_key.get(fk) or by_key.get((key, "false", ""))
                    self.add("EXC-004", "%s on the same path as set_false_path at %s:%d; the false "
                             "path wins" % (e["cmd"], fp["loc"].file, fp["loc"].line), loc,
                             sev=SEV_INFO)
        for key, e, mult in setup_mcp:
            if key not in hold_keys:
                self.add("EXC-003", "set_multicycle_path %g (setup) without a matching -hold "
                         "multicycle: hold is checked %g cycle(s) earlier (usually needs -hold %g)"
                         % (mult, mult - 1, mult - 1), e["loc"])
        # redundancy with clock groups
        pairs = set()
        allclk = set(m.clocks)
        for g in m.clock_groups:
            gs = g["groups"]
            if len(gs) == 1:
                gs = gs + [allclk - gs[0]]
            for i, a in enumerate(gs):
                for j, b in enumerate(gs):
                    if i != j:
                        for x in a:
                            for y in b:
                                pairs.add((x, y))
        if pairs:
            for e in m.exceptions:
                if e["kind"] != "false" or e["dropped"] or e["thr"]:
                    continue
                if e["frm"] and e["to"] and all(k[0] == "clock" for k in e["frm"] + e["to"]):
                    if all((a[1], b[1]) in pairs for a in e["frm"] for b in e["to"]):
                        self.add("EXC-005", "set_false_path between clocks already made exclusive/"
                                 "asynchronous by set_clock_groups", e["loc"])


def cross_mode_checks(modes, d, mx):
    out = []
    if len(modes) < 2:
        return out
    names = [m.name for m in modes]

    def add(rule, msg, obj=""):
        out.append(Finding(rule, RULES[rule][0], "*", "", 0, "", obj, msg))

    allck = collections.OrderedDict()
    for m in modes:
        for c in m.clocks:
            allck.setdefault(c, []).append(m)
    for c, ms in allck.items():
        if len(ms) != len(modes):
            add("MODE-001", "clock %s defined in [%s] but not in [%s]"
                % (c, ", ".join(x.name for x in ms),
                   ", ".join(n for n in names if n not in [x.name for x in ms])), c)
        sig = {}
        for m in ms:
            ck = m.clocks[c]
            srcs = tuple(sorted(str(s) for s in ck.sources))
            sig.setdefault((ck.period, tuple(ck.waveform), srcs), []).append(m.name)
        if len(sig) > 1:
            add("MODE-002", "clock %s differs across modes: %s" % (c, "; ".join(
                "%s: period=%s sources=%d" % ("/".join(v), k[0], len(k[2])) for k, v in sig.items())), c)
    for kind, rule, what in (("in", "MODE-003", "input delay"), ("out", "MODE-003", "output delay")):
        pat = collections.defaultdict(list)
        for i, p in enumerate(d.ports):
            have = tuple(m.name for m in modes if i in getattr(m, "io_cov", {}).get(kind, ()))
            if have and len(have) != len(modes):
                pat[have].append(p.name)
        for have, ports in pat.items():
            add(rule, "%d port(s) have %s in [%s] but not in [%s]" % (
                len(ports), what, ", ".join(have), ", ".join(n for n in names if n not in have)),
                " ".join(compress_names(ports, mx)))
    for attr, what in (("drive_ports", "drive/transition"), ("load_ports", "set_load")):
        pat = collections.defaultdict(list)
        for i, p in enumerate(d.ports):
            have = tuple(m.name for m in modes if i in getattr(m, attr))
            if have and len(have) != len(modes):
                pat[have].append(p.name)
        for have, ports in pat.items():
            add("MODE-004", "%d port(s) have %s in [%s] but not in [%s]" % (
                len(ports), what, ", ".join(have), ", ".join(n for n in names if n not in have)),
                " ".join(compress_names(ports, mx)))
    return out


# =============================================================================
# 8. Reporting
# =============================================================================

def write_reports(out_dir, findings, summary):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    findings = sorted(findings, key=lambda f: (f.mode != "*", f.mode, _SEV_RANK[f.sev], f.rule,
                                               f.file, f.line))

    # Short labels for SDC files in the .rpt body: basename, disambiguated with
    # parent directories only when two files share a basename.
    all_files = sorted(set(f.file for f in findings if f.file) |
                       set(p for m in summary["modes"] for p in m["files"]))
    short_name = {}
    by_base = collections.defaultdict(list)
    for p in all_files:
        by_base[os.path.basename(p)].append(p)
    for base, paths in by_base.items():
        if len(paths) == 1:
            short_name[paths[0]] = base
        else:
            parts = [p.split(os.sep) for p in paths]
            n = 1
            while n < max(len(p) for p in parts):
                tails = [os.sep.join(p[-n - 1:]) for p in parts]
                if len(set(tails)) == len(tails):
                    break
                n += 1
            for p, tail in zip(paths, [os.sep.join(p[-n - 1:]) for p in parts]):
                short_name[p] = tail

    import csv
    with open(os.path.join(out_dir, "sdc_qc.csv"), "w") as fh:
        w = csv.writer(fh)
        w.writerow(["rule", "severity", "mode", "file", "line", "command", "objects", "message"])
        for f in findings:
            w.writerow([f.rule, f.sev, f.mode, f.file, f.line, f.cmd, f.obj, f.msg])
    with open(os.path.join(out_dir, "sdc_qc.json"), "w") as fh:
        json.dump({"summary": summary, "findings": [f.as_dict() for f in findings]}, fh, indent=1)
    L = []
    L.append("SDC QC report  (sdc_qc %s)  %s" % (VERSION, time.strftime("%Y-%m-%d %H:%M:%S")))
    L.append("=" * 100)
    L.append("Top: %s   cells: %s   nets: %s   ports: %s" % (
        summary["top"], summary["cells"], summary["nets"], summary["ports"]))
    L.append("Liberty time unit: %s   capacitance unit: %s" % (summary["time_unit"],
                                                             summary["cap_unit"]))
    for m in summary["modes"]:
        L.append("Mode %-16s clocks=%-4d files: %s" % (
            m["name"], m["clocks"], " ".join(short_name.get(p, p) for p in m["files"])))
        if m.get("coverage"):
            cv = m["coverage"]
            L.append("     clock pins reached by a clock: %d / %d" % (cv["clocked"], cv["total"]))
    L.append("")
    if short_name:
        L.append("Files")
        for p in sorted(short_name, key=lambda p: short_name[p]):
            L.append("  %-30s %s" % (short_name[p], p))
        L.append("")
    L.append("Summary (count of findings)")
    modes = [m["name"] for m in summary["modes"]] + (["*"] if any(f.mode == "*" for f in findings) else [])
    L.append("  %-10s" % "severity" + "".join("%12s" % m[:12] for m in modes))
    for sev in (SEV_ERROR, SEV_WARNING, SEV_INFO):
        L.append("  %-10s" % sev + "".join("%12d" % sum(1 for f in findings if f.sev == sev and f.mode == m)
                                           for m in modes))
    L.append("")
    L.append("By rule")
    cnt = collections.Counter((f.rule, f.sev) for f in findings)
    for (rule, sev), n in sorted(cnt.items(), key=lambda kv: (_SEV_RANK[kv[0][1]], kv[0][0])):
        L.append("  %-10s %-8s %6d  %s" % (rule, sev, n, RULES[rule][1]))
    for m in modes:
        L.append("")
        L.append("-" * 100)
        L.append("Mode: %s" % (m if m != "*" else "* (cross-mode)"))
        L.append("-" * 100)
        for f in findings:
            if f.mode != m:
                continue
            fshort = short_name.get(f.file, f.file)
            where = ("%s:%d" % (fshort, f.line) if f.line else fshort) if f.file else ""
            L.append("%-7s %-9s %s %s" % (f.sev, f.rule, where, f.msg))
            if f.cmd:
                L.append("        cmd: %s" % f.cmd)
            if f.obj:
                o = f.obj if len(f.obj) < 600 else f.obj[:600] + " ...(see csv/json)"
                L.append("        obj: %s" % o)
    L.append("")
    L.append("Timing (s): " + ", ".join("%s=%.1f" % kv for kv in summary["timing"].items()))
    with open(os.path.join(out_dir, "sdc_qc.rpt"), "w") as fh:
        fh.write("\n".join(L) + "\n")


# =============================================================================
# 9. Command line
# =============================================================================

def parse_mode(spec):
    """'name:VAR=val:a.sdc:b.sdc' -> (name, [('var',VAR,val) | ('sdc',path,None)...])"""
    raw = spec.split(":")
    parts = raw[:1]
    for p in raw[1:]:
        # re-join a Windows drive letter: "D" + "\\path" or "/path" -> "D:\\path"
        if len(parts) > 1 and re.match(r"^[A-Za-z]$", parts[-1]) and p[:1] in ("\\", "/"):
            parts[-1] += ":" + p
        else:
            parts.append(p)
    if len(parts) < 2 or not parts[0]:
        _fatal("ERROR: bad -mode '%s' (expected name[:VAR=value...]:file.sdc[:more.sdc])"
                         % spec)
    steps = []
    for p in parts[1:]:
        p = p.strip()
        if not p:
            continue
        if "=" in p and not os.path.exists(p):
            k, v = p.split("=", 1)
            steps.append(("var", k.strip(), v.strip()))
        else:
            steps.append(("sdc", p, None))
    if not any(s[0] == "sdc" for s in steps):
        _fatal("ERROR: -mode '%s' has no SDC file" % spec)
    return parts[0], steps


def _run_mode(args):
    """Runs one mode (in the main process or a forked worker). Uses the global
    design built before forking."""
    name, steps, opts = args
    d = _G["design"]
    t0 = time.time()
    eng = SdcEngine(d, name, opts)
    try:
        m = eng.run(steps)
        ModeChecker(eng, _G["reg_slots"], opts).run()
    except Exception as e:  # keep other modes running
        import traceback
        eng.add("PARSE-001", "internal error in mode %s: %r\n%s" % (name, e, traceback.format_exc()),
                Loc("", 0, ""))
        m = eng.m
    m.seconds = time.time() - t0
    lite = {"name": m.name, "clocks": collections.OrderedDict(
        (c.name, (c.period, c.waveform, [str(s) for s in c.sources])) for c in m.clocks.values()),
        "files": m.files, "coverage": getattr(m, "coverage", None),
        "io_cov": {k: sorted(v) for k, v in getattr(m, "io_cov", {"in": (), "out": ()}).items()},
        "drive_ports": sorted(m.drive_ports), "load_ports": sorted(m.load_ports),
        "seconds": m.seconds}
    return lite, eng.findings


class _LiteMode(object):
    def __init__(self, lite):
        self.name = lite["name"]
        self.clocks = collections.OrderedDict()
        for n, (per, wf, srcs) in lite["clocks"].items():
            ck = Clock(n, per, wf, srcs, False, None, None)
            self.clocks[n] = ck
        self.io_cov = {k: set(v) for k, v in lite["io_cov"].items()}
        self.drive_ports, self.load_ports = set(lite["drive_ports"]), set(lite["load_ports"])


_G = {}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="sdc_qc.py", allow_abbrev=False,
        description="Static SDC QC against a gate-level netlist and Liberty (pre-APR).")
    ap.add_argument("-netlist", nargs="+", action="append", required=True, metavar="V",
                    help="gate-level Verilog netlist(s) (.v or .v.gz); top + optional sub-blocks")
    ap.add_argument("-lib", nargs="+", action="append", default=[], metavar="LIB",
                    help="Liberty file(s) (.lib or .lib.gz)")
    ap.add_argument("-lib_list", action="append", default=[], metavar="FILE",
                    help="text file with one Liberty path per line (# comments allowed)")
    ap.add_argument("-mode", action="append", required=True, metavar="SPEC",
                    help="'name[:VAR=value...]:file.sdc[:incr.sdc...]' ; VAR=value is set as a "
                         "global Tcl variable before the following files are sourced, in order")
    ap.add_argument("-top", help="top module (default: auto-detect)")
    ap.add_argument("-blackbox", action="append", default=[], metavar="PATTERN",
                    help="treat unresolved references matching PATTERN (glob) as black boxes")
    ap.add_argument("-search_path", nargs="+", action="append", default=[], metavar="DIR",
                    help="directories searched by 'source' for relative paths")
    ap.add_argument("-out_dir", default="sdc_qc_out", help="report directory (default sdc_qc_out)")
    ap.add_argument("-jobs", type=int, default=min(8, os.cpu_count() or 1),
                    help="parallel processes for Liberty parsing, netlist elaboration and modes "
                         "(default min(8,cpus))")
    ap.add_argument("-cache_dir", default=os.path.join(os.path.expanduser("~"), ".cache", "sdc_qc"),
                    help="Liberty extract cache directory (default ~/.cache/sdc_qc)")
    ap.add_argument("-no_cache", action="store_true", help="do not read/write the Liberty cache")
    ap.add_argument("-io_threshold", type=float, default=0.8,
                    help="flag I/O delays >= this fraction of the clock period (default 0.8)")
    ap.add_argument("-max_examples", type=int, default=20,
                    help="example objects listed per finding (default 20)")
    ap.add_argument("-no_clock_trace", action="store_true",
                    help="skip the register clock-pin coverage trace")
    ap.add_argument("-version", action="version", version="sdc_qc " + VERSION)
    opts = ap.parse_args(argv)
    opts.netlist = [x for grp in opts.netlist for x in grp]
    opts.search_path = [x for grp in opts.search_path for x in grp]
    libs = [x for grp in opts.lib for x in grp]
    for lf in opts.lib_list:
        if not os.path.isfile(lf):
            _fatal("ERROR: -lib_list file not found: %s" % lf)
        with open(lf) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    libs.append(line)
    modes = [parse_mode(s) for s in opts.mode]
    if len(set(n for n, _ in modes)) != len(modes):
        _fatal("ERROR: duplicate mode names")
    try:
        __import__("tkinter")
    except ImportError:
        _fatal("ERROR: this Python has no tkinter (needed for the Tcl interpreter)")

    timing = collections.OrderedDict()
    t0 = time.time()
    lib_ex = load_liberties(libs, opts.jobs, None if opts.no_cache else opts.cache_dir)
    timing["liberty"] = time.time() - t0
    findings = []
    for lib in lib_ex:
        for e in lib["errors"]:
            findings.append(Finding("LIB-001", SEV_ERROR, "*", lib["path"], 0, "", "", e))
        _log("liberty %s: %d cells, %.0f MB, %.1fs%s" % (
            os.path.basename(lib["path"]), len(lib["cells"]), lib.get("bytes", 0) / 1e6,
            lib.get("seconds", 0), " (cache)" if lib.get("cached") else ""))
    if not libs:
        _log("warning: no Liberty given; every cell is unresolved")
    t0 = time.time()
    d = build_design(opts.netlist, lib_ex, opts.top, opts.blackbox, opts.jobs)
    timing["netlist"] = time.time() - t0
    for rule, msg in d.issues:
        findings.append(Finding(rule, RULES[rule][0], "*", "", 0, "", "", msg))
    t0 = time.time()
    _G["design"] = d
    _G["reg_slots"] = register_clock_slots(d) if not opts.no_clock_trace else array.array("q")
    timing["prep"] = time.time() - t0
    t0 = time.time()
    mode_args = [(n, s, opts) for n, s in modes]
    results = []
    if opts.jobs > 1 and len(modes) > 1 and hasattr(os, "fork"):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(min(opts.jobs, len(modes))) as pool:
            results = pool.map(_run_mode, mode_args, chunksize=1)
    else:
        results = [_run_mode(a) for a in mode_args]
    timing["modes"] = time.time() - t0
    lites = []
    for lite, fs in results:
        findings.extend(fs)
        lites.append(lite)
        _log("mode %s: %d clocks, %d findings, %.1fs" % (lite["name"], len(lite["clocks"]),
                                                         len(fs), lite["seconds"]))
    findings.extend(cross_mode_checks([_LiteMode(l) for l in lites], d, opts.max_examples))
    summary = {
        "version": VERSION, "top": d.top, "cells": len(d.cell_names), "nets": len(d.net_names),
        "ports": len(d.ports), "time_unit": d.time_unit, "cap_unit": d.cap_unit,
        "netlists": opts.netlist, "libs": libs,
        "modes": [{"name": l["name"], "clocks": len(l["clocks"]), "files": l["files"],
                   "coverage": l["coverage"], "seconds": round(l["seconds"], 2)} for l in lites],
        "timing": timing,
        "counts": dict(collections.Counter(f.sev for f in findings)),
    }
    write_reports(opts.out_dir, findings, summary)
    c = summary["counts"]
    _log("done: %d error(s), %d warning(s), %d info -> %s/sdc_qc.rpt" % (
        c.get(SEV_ERROR, 0), c.get(SEV_WARNING, 0), c.get(SEV_INFO, 0), opts.out_dir))
    return 1 if c.get(SEV_ERROR, 0) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _fatal("interrupted")
    except Exception:
        import traceback
        traceback.print_exc()
        _fatal("ERROR: internal error in sdc_qc %s (please report with the traceback above)"
               % VERSION)
