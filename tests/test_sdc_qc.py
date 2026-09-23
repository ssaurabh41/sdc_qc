#!/usr/bin/env python3
"""Regression tests for sdc_qc.py (unittest only; Python 3.6+ with tkinter).

    python3 tests/test_sdc_qc.py -v
"""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
sys.path.insert(0, os.path.dirname(HERE))
import sdc_qc  # noqa: E402


def run(modes, extra=(), libs=("cells.lib",), netlists=("foo.v", "sub.v")):
    out = tempfile.mkdtemp(prefix="sdcqc_")
    argv = ["-netlist"] + [os.path.join(DATA, n) for n in netlists]
    if libs:
        argv += ["-lib"] + [l if os.path.isabs(l) else os.path.join(DATA, l) for l in libs]
    for m in modes:
        argv += ["-mode", m]
    argv += ["-out_dir", out, "-no_cache", "-jobs", "1"] + list(extra)
    rc = sdc_qc.main(argv)
    with open(os.path.join(out, "sdc_qc.json")) as fh:
        res = json.load(fh)
    shutil.rmtree(out)
    return rc, res


def D(name):
    return os.path.join(DATA, name)


class LibertyTests(unittest.TestCase):
    def test_extract(self):
        lib = sdc_qc.parse_liberty(D("cells.lib"))
        self.assertEqual(lib["errors"], [])
        self.assertEqual(lib["name"], "tcells")
        self.assertEqual(lib["time_unit"], "1ns")
        cells = lib["cells"]
        # test_cell and pg_pin content must not leak into the cell
        self.assertEqual(sorted(cells["DFFX1"]["pins"]), ["CK", "D", "Q"])
        self.assertEqual(cells["DFFX1"]["seq"], "ff")
        self.assertEqual(sorted(cells["AND2X1"]["pins"]), ["A1", "A2", "Y"])
        self.assertEqual(cells["SRAM16X8"]["bus"]["A"], ["A[3]", "A[2]", "A[1]", "A[0]"])
        self.assertTrue(cells["SRAM16X8"]["memory"])
        self.assertEqual(cells["SRAM16X8"]["pins"]["Q[5]"]["dir"], "output")

    def test_refs(self):
        lib = sdc_qc.parse_liberty(D("cells.lib"))
        c = lib["cells"]
        inv = sdc_qc.ref_from_lib("INVX1", c["INVX1"], "x")
        self.assertTrue(inv.inv and inv.buf_in == inv.pidx["A"])
        buf = sdc_qc.ref_from_lib("BUFX2", c["BUFX2"], "x")
        self.assertFalse(buf.inv)
        self.assertEqual(buf.buf_in, buf.pidx["A"])
        dff = sdc_qc.ref_from_lib("DFFX1", c["DFFX1"], "x")
        self.assertEqual(dff.clock_pins, frozenset([dff.pidx["CK"]]))
        self.assertEqual(dff.endpoint_pins, frozenset([dff.pidx["D"]]))
        icg = sdc_qc.ref_from_lib("ICGX1", c["ICGX1"], "x")
        self.assertEqual(icg.icg_in, icg.pidx["CK"])
        ram = sdc_qc.ref_from_lib("SRAM16X8", c["SRAM16X8"], "x")
        self.assertEqual(ram.clock_pins, frozenset([ram.pidx["CLK"]]))
        self.assertIn(ram.pidx["D[3]"], ram.endpoint_pins)
        mux = sdc_qc.ref_from_lib("MUX2X1", c["MUX2X1"], "x")
        self.assertEqual(mux.buf_in, -1)

    def test_gzip_and_chunking(self):
        tmp = tempfile.mkdtemp()
        try:
            gz = os.path.join(tmp, "cells.lib.gz")
            with open(D("cells.lib"), "rb") as f, gzip.open(gz, "wb") as g:
                g.write(f.read())
            old = sdc_qc._READ_CHUNK
            sdc_qc._READ_CHUNK = 7          # force every boundary case
            try:
                a = sdc_qc.parse_liberty(gz)
            finally:
                sdc_qc._READ_CHUNK = old
            b = sdc_qc.parse_liberty(D("cells.lib"))
            self.assertEqual(a["cells"], b["cells"])
            self.assertEqual(a["errors"], [])
        finally:
            shutil.rmtree(tmp)

    def test_cache(self):
        tmp = tempfile.mkdtemp()
        try:
            r1 = sdc_qc.load_liberties([D("cells.lib")], 1, tmp)
            r2 = sdc_qc.load_liberties([D("cells.lib")], 1, tmp)
            self.assertFalse(r1[0]["cached"])
            self.assertTrue(r2[0]["cached"])
            self.assertEqual(r1[0]["cells"], r2[0]["cells"])
        finally:
            shutil.rmtree(tmp)


class NetlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        libs = sdc_qc.load_liberties([D("cells.lib")], 1, None)
        cls.d = sdc_qc.build_design([D("foo.v"), D("sub.v")], libs, None, ["hardip"])

    def test_top_and_hierarchy(self):
        d = self.d
        self.assertEqual(d.top, "foo")
        self.assertIn("u_sub/r_reg[2]", d.cell_index)
        self.assertIn("u_core/q_reg", d.cell_index)      # escaped flat name

    def test_connectivity(self):
        d = self.d
        c = d.cell_index["u_sub/r_reg[0]"]
        r = d.refs[d.cell_ref[c]]
        n = d.slot_net(d.cell_off[c] + r.pidx["D"])
        # din[0] of u_sub is {a[3:1], n3}[0] = n3
        self.assertEqual(d.net_names[n], "n3")
        m = d.cell_index["u_mem"]
        rm = d.refs[d.cell_ref[m]]
        self.assertEqual(d.net_names[d.slot_net(d.cell_off[m] + rm.pidx["D[7]"])], "b[3]")
        self.assertEqual(d.net_names[d.slot_net(d.cell_off[m] + rm.pidx["A[0]"])], "addr[0]")
        t = d.cell_index["u_core/tie_reg"]
        self.assertEqual(d.conn[d.cell_off[t] + d.refs[d.cell_ref[t]].pidx["CK"]], sdc_qc.CONST0)

    def test_assign_union(self):
        d = self.d
        self.assertEqual(d.find(d.net_index["z"]), d.find(d.net_index["u_core/q"]))

    def test_blackbox(self):
        self.assertIn(("DES-003", "reference hardip black-boxed"), self.d.issues)


def rules(res, mode=None):
    out = {}
    for f in res["findings"]:
        if mode is None or f["mode"] == mode:
            out.setdefault(f["rule"], []).append(f)
    return out


class SdcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rc, cls.res = run([
            "mode1:SDC_MODE=mode1:%s:%s" % (D("foo.nondft.sdc"), D("bar.sdc")),
            "mode2:SDC_MODE=mode2:%s" % D("foo.nondft.sdc"),
            "dft:%s" % D("foo.dft.sdc")])

    def test_exit_code(self):
        self.assertEqual(self.rc, 1)

    def test_mode_variables_and_incremental(self):
        m = dict((x["name"], x) for x in self.res["summary"]["modes"])
        self.assertEqual(len(m["mode1"]["files"]), 2)          # incremental bar.sdc sourced
        self.assertEqual(len(m["mode2"]["files"]), 1)
        mode2 = rules(self.res, "*")["MODE-002"][0]["msg"]      # CLK period from SDC_MODE
        self.assertIn("period=4.0", mode2)

    def test_expected_findings_mode1(self):
        r = rules(self.res, "mode1")
        lines = lambda rule: sorted(f["line"] for f in r.get(rule, []))
        self.assertEqual(lines("OBJ-001"), [30])
        self.assertEqual(lines("OBJ-002"), [30])
        self.assertEqual(lines("PARSE-002"), [29])              # y[0] unbraced -> [0]
        self.assertEqual(lines("IO-003"), [29])
        self.assertEqual(lines("EXC-001"), [31])
        self.assertEqual(lines("EXC-003"), [32])
        self.assertEqual(lines("EXC-005"), [34])
        self.assertEqual(lines("CASE-001"), [36])
        self.assertEqual(lines("MISC-001"), [37])
        self.assertEqual(lines("CLK-010"), [17])
        self.assertEqual(lines("CLK-012"), [44])
        self.assertEqual(lines("PARSE-004"), [4])               # in bar.sdc
        self.assertTrue(r["PARSE-004"][0]["file"].endswith("bar.sdc"))
        self.assertNotIn("PARSE-001", r)                          # foreach/filter/procs ok
        self.assertIn("rst_n", r["IO-001"][0]["obj"])
        self.assertEqual(r["COV-003"][0]["obj"], "u_core/tie_reg/CK")
        self.assertEqual(r["COV-002"][0]["obj"], "u_core/mux_reg/CK")
        self.assertNotIn("COV-001", r)                            # DIV2 covers slow_reg
        self.assertNotIn("UNIT-001", r)

    def test_expected_findings_dft(self):
        r = rules(self.res, "dft")
        self.assertIn("CLK-001", r)
        self.assertEqual(r["OBJ-001"][0]["line"], 10)
        self.assertNotIn("EXC-003", r)                            # -hold companion present
        roots = " | ".join(f["msg"] for f in r["COV-001"])
        self.assertIn("input port clk2", roots)
        self.assertIn("u_core/div_reg/Q", roots)

    def test_unresolved_reference(self):
        r = rules(self.res, "*")
        self.assertIn("hardip", r["DES-002"][0]["msg"])


class EngineUnitTests(unittest.TestCase):
    """Small SDC snippets run through a real engine."""

    @classmethod
    def setUpClass(cls):
        libs = sdc_qc.load_liberties([D("cells.lib")], 1, None)
        cls.d = sdc_qc.build_design([D("foo.v"), D("sub.v")], libs, None, ["hardip"])
        sdc_qc._G["design"] = cls.d
        sdc_qc._G["reg_slots"] = sdc_qc.register_clock_slots(cls.d)

    def eng(self, text):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "t.sdc")
        with open(p, "w") as fh:
            fh.write(text)

        class O(object):
            search_path, max_examples, io_threshold, no_clock_trace = [], 20, 0.8, False
        e = sdc_qc.SdcEngine(self.d, "t", O())
        e.run([("sdc", p, None)])
        shutil.rmtree(tmp)
        return e

    def ev(self, e, cmd):
        return e.sdc_eval(cmd)

    def test_patterns(self):
        e = self.eng("")
        self.assertEqual(self.ev(e, "sizeof_collection [get_ports a]"), "4")      # bus base
        self.assertEqual(self.ev(e, "sizeof_collection [get_ports {a[1]}]"), "1")
        self.assertEqual(self.ev(e, "sizeof_collection [get_cells *]"), "10")     # top level only
        self.assertEqual(self.ev(e, "sizeof_collection [get_cells -hier *reg*]"), "13")
        self.assertEqual(self.ev(e, "sizeof_collection [get_pins -hier */CK]"), "15")
        self.assertEqual(e.tcl.splitlist(self.ev(e, "get_object_name [get_pins u_mem/A]")),
                         ("u_mem/A[3]", "u_mem/A[2]", "u_mem/A[1]", "u_mem/A[0]"))
        self.assertEqual(self.ev(e, "sizeof_collection [get_nets -hier gclk]"), "1")
        self.assertEqual(self.ev(e, "sizeof_collection [get_cells -regexp {u_core/.*_reg}]"), "5")

    def test_filter_and_attributes(self):
        e = self.eng("")
        self.assertEqual(self.ev(e, 'sizeof_collection [get_ports * -filter "direction==in"]'),
                         "15")
        self.assertEqual(self.ev(e, 'sizeof_collection [get_cells -hier * -filter '
                                    '"is_sequential && !(ref_name =~ SRAM*)"]'), "14")  # 13 DFF + ICG
        self.assertEqual(self.ev(e, "get_attribute [get_cells u_mem] ref_name"), "SRAM16X8")
        self.assertEqual(self.ev(e, "get_attribute [get_pins u_core/q_reg/CK] is_clock_pin"),
                         "true")

    def test_of_objects(self):
        e = self.eng("")
        self.assertEqual(self.ev(e, "get_object_name [get_cells -of [get_pins u_sub/u_icg/E]]"),
                         "u_sub/u_icg")
        self.assertEqual(self.ev(e, "get_object_name [get_nets -of [get_pins cki/Y]]"), "ckinv")
        pins = e.tcl.splitlist(self.ev(e, "get_object_name [get_pins -leaf -of [get_nets ckinv]]"))
        self.assertEqual(sorted(pins), ["cki/Y", "u_core/q_reg/CK"])

    def test_all_registers_clock(self):
        e = self.eng("create_clock -name C -period 1 [get_ports clk]\n")
        regs = e.tcl.splitlist(self.ev(e, "get_object_name [all_registers -clock C]"))
        self.assertIn("u_sub/r_reg[0]", regs)
        self.assertNotIn("u_sub/u_icg", regs)        # ICGs are not registers
        self.assertIn("u_core/q_reg", regs)          # through BUF + INV
        self.assertNotIn("u_core/div_reg", regs)

    def test_line_numbers_in_loops_and_procs(self):
        e = self.eng("set x 1\n\nforeach p {nope1 nope2} {\n  get_ports $p\n}\n"
                     "proc f {} {\n  get_ports nope3\n}\nf\n")
        lines = sorted(f.line for f in e.findings if f.rule == "OBJ-001")
        self.assertEqual(lines, [4, 4, 9])

    def test_error_recovery(self):
        e = self.eng("create_clock -period 1 -name A [get_ports clk]\nfoo_bar 1 2\n"
                     "set_input_delay -clock A 0.1 [get_ports a]\nexpr {1/0}\n"
                     "set_output_delay -clock A 0.1 [get_ports z]\n")
        r = dict((f.rule, f.line) for f in e.findings)
        self.assertEqual(r["PARSE-002"], 2)
        self.assertEqual(r["PARSE-001"], 4)
        self.assertEqual(len(e.m.io), 2)              # execution continued after errors

    def test_dropped_io_delay_not_counted(self):     # finding 2
        e = self.eng("create_clock -name C -period 2 [get_ports clk]\n"
                     "set_input_delay -clock MISSING 0.2 [get_ports a]\n")
        self.assertEqual(e.m.io, [])
        self.assertIn("OBJ-002", [f.rule for f in e.findings])

    def test_pin_io_delay_diagnosed(self):           # finding 3
        e = self.eng("create_clock -name C -period 2 [get_ports clk]\n"
                     "set_input_delay -clock C 0.3 [get_pins u_core/q_reg/D]\n")
        self.assertEqual([f.obj for f in e.findings if f.rule == "IO-009"],
                         ["u_core/q_reg/D"])

    def test_sandbox(self):
        tmp = tempfile.mkdtemp()
        try:
            keep = os.path.join(tmp, "keep").replace("\\", "/")
            new = os.path.join(tmp, "new").replace("\\", "/")
            open(keep, "w").close()
            cmds = ["exec rm -rf %s" % keep, "set f [open %s w]" % new,
                    "set f [__qc_tcl_open %s w]" % new, "__qc_tcl_file delete %s" % keep,
                    "__qc_tcl_puts stdout x", "interp invokehidden {} open %s w" % new,
                    "file delete %s" % keep, "glob %s/*" % tmp]
            e = self.eng("\n".join(cmds) + "\n")
            self.assertEqual(sum(1 for f in e.findings
                                 if f.rule in ("PARSE-001", "PARSE-002")), len(cmds))
            self.assertTrue(os.path.exists(keep))
            self.assertFalse(os.path.exists(new))
            self.assertEqual(self.ev(e, "file tail a/b.sdc"), "b.sdc")   # path helpers kept
        finally:
            shutil.rmtree(tmp)

    def test_non_finite_numbers(self):
        e = self.eng("create_clock -name C -period NaN [get_ports clk]\n"
                     "set_clock_uncertainty Inf [get_ports clk]\n"
                     "set_multicycle_path -Inf -to [get_pins u_core/q_reg/D]\n"
                     "set_input_delay -clock C NaN [get_ports a]\n"
                     "create_generated_clock -name G -source [get_ports clk] -divide_by Inf "
                     "[get_pins u_core/div_reg/Q]\n")
        r = sorted((f.rule, f.line) for f in e.findings if f.rule in ("CLK-003", "PARSE-003",
                                                                       "CLK-009"))
        self.assertEqual(r, [("CLK-003", 1), ("CLK-009", 5), ("PARSE-003", 2), ("PARSE-003", 3),
                             ("PARSE-003", 4), ("PARSE-003", 5)])

    def test_generated_clock_checks(self):
        e = self.eng("create_clock -name C -period 2 [get_ports clk]\n"
                     "create_generated_clock -name G -source [get_ports clk] -divide_by 3 "
                     "-multiply_by 2 [get_pins u_core/div_reg/Q]\n"
                     "create_generated_clock -name G2 -source [get_ports clk] -edges {1 2} "
                     "[get_pins u_core/slow_reg/Q]\n"
                     "create_generated_clock -name G3 -master_clock NOPE -source [get_ports clk] "
                     "-divide_by 2 [get_pins u_core/mux_reg/Q]\n")
        r = [f.rule for f in e.findings]
        self.assertEqual(r.count("CLK-009"), 2)
        self.assertIn("OBJ-001", r)          # NOPE
        self.assertAlmostEqual(e.m.clocks["G"].period, 6.0)

    def test_units(self):
        e = self.eng("set_units -time ps\ncreate_clock -name C -period 1000 [get_ports clk]\n")
        sdc_qc.ModeChecker(e, sdc_qc._G["reg_slots"], type("O", (), {
            "no_clock_trace": True, "max_examples": 5, "io_threshold": 0.8})()).run()
        self.assertIn("UNIT-001", [f.rule for f in e.findings])

    def test_parse_proc_arguments(self):
        e = self.eng("proc p args {\n parse_proc_arguments -args $args r\n"
                     " get_ports $r(-port)\n}\n"
                     "define_proc_attributes p -define_args {{-port \"p\" name string required}"
                     " {-v \"v\" \"\" boolean optional}}\np -v -port nope9\n")
        self.assertEqual([f.obj for f in e.findings if f.rule == "OBJ-001"], ["nope9"])


class BenchTests(unittest.TestCase):
    """544-instance, 6-clock design (generated clock, clock mux) with a clean SDC:
    guards against false positives. Must stay at zero findings."""

    def test_clean_benchmark(self):
        rc, res = run(["func:%s" % D("bench.sdc")], netlists=("bench.v",))
        self.assertEqual(rc, 0)
        self.assertEqual([(f["rule"], f["msg"]) for f in res["findings"]], [])
        cov = res["summary"]["modes"][0]["coverage"]
        self.assertEqual(cov["clocked"], cov["total"])


class ParallelTests(unittest.TestCase):
    """The parallel paths (forked elaboration workers, parallel modes/libs) must
    give exactly the serial result."""

    def test_parallel_elaboration_matches_serial(self):
        libs = sdc_qc.load_liberties([D("cells.lib")], 1, None)
        ser = sdc_qc.build_design([D("foo.v"), D("sub.v")], libs, None, ["hardip"], jobs=1)
        old = sdc_qc._PAR_ELAB_BYTES
        sdc_qc._PAR_ELAB_BYTES = 0
        try:
            par = sdc_qc.build_design([D("foo.v"), D("sub.v")], libs, None, ["hardip"], jobs=3)
        finally:
            sdc_qc._PAR_ELAB_BYTES = old

        def pins(d):
            out = {}
            for c, name in enumerate(d.cell_names):
                r = d.refs[d.cell_ref[c]]
                for p in range(d.ncell_pins(c)):
                    n = d.conn[d.cell_off[c] + p]
                    out[(name, r.pins[p])] = d.net_names[d.find(n)] if n >= 0 else n
            return out
        self.assertEqual(pins(ser), pins(par))

    def test_parallel_modes_match_serial(self):
        modes = ["mode1:SDC_MODE=mode1:%s:%s" % (D("foo.nondft.sdc"), D("bar.sdc")),
                 "dft:%s" % D("foo.dft.sdc")]
        _, a = run(modes)
        _, b = run(modes, extra=["-jobs", "2"])
        key = lambda r: sorted((f["rule"], f["mode"], f["line"], f["msg"]) for f in r["findings"])
        self.assertEqual(key(a), key(b))


class Findings1Tests(unittest.TestCase):
    """Regression tests for the independent review in codex/findings-1."""

    def design(self, text, jobs=1):
        tmp = tempfile.mkdtemp()
        try:
            v = os.path.join(tmp, "t.v")
            with open(v, "w") as fh:
                fh.write(text)
            libs = sdc_qc.load_liberties([D("cells.lib")], 1, None)
            return sdc_qc.build_design([v], libs, None, [], jobs=jobs)
        finally:
            shutil.rmtree(tmp)

    def test_instance_array_expanded(self):          # finding 1
        d = self.design("module t (clk, d, q); input clk; input [3:0] d; output [3:0] q;\n"
                        "DFFX1 u[3:0] (.CK(clk), .D(d), .Q(q));\nendmodule\n")
        self.assertEqual(sorted(d.cell_names), ["u[0]", "u[1]", "u[2]", "u[3]"])
        c = d.cell_index["u[3]"]
        r = d.refs[d.cell_ref[c]]
        self.assertEqual(d.net_names[d.slot_net(d.cell_off[c] + r.pidx["D"])], "d[3]")
        self.assertEqual(d.net_names[d.slot_net(d.cell_off[c] + r.pidx["CK"])], "clk")
        self.assertEqual(len(sdc_qc.register_clock_slots(d)), 4)

    def test_no_fork_serial_fallback(self):         # finding 5
        old = (sdc_qc._HAS_FORK, sdc_qc._PAR_ELAB_BYTES)
        sdc_qc._HAS_FORK, sdc_qc._PAR_ELAB_BYTES = False, 0
        try:
            d = self.design("module t (clk, d, q); input clk, d; output q;\n"
                            "DFFX1 u (.CK(clk), .D(d), .Q(q));\nendmodule\n", jobs=4)
        finally:
            sdc_qc._HAS_FORK, sdc_qc._PAR_ELAB_BYTES = old
        self.assertEqual(d.cell_names, ["u"])

    def test_windows_mode_paths(self):              # finding 4
        n, s = sdc_qc.parse_mode("m:SDC_MODE=x:D:\\w\\a.sdc:C:/w/b.sdc:/unix/c.sdc")
        self.assertEqual([x[1] for x in s], ["SDC_MODE", "D:\\w\\a.sdc", "C:/w/b.sdc",
                                             "/unix/c.sdc"])


class ModeSpecTests(unittest.TestCase):
    def test_parse_mode(self):
        n, s = sdc_qc.parse_mode("m1:SDC_MODE=mode1:a.sdc:b.sdc")
        self.assertEqual(n, "m1")
        self.assertEqual(s, [("var", "SDC_MODE", "mode1"), ("sdc", "a.sdc", None),
                             ("sdc", "b.sdc", None)])
        n, s = sdc_qc.parse_mode("dft:x.sdc")
        self.assertEqual(s, [("sdc", "x.sdc", None)])
        with self.assertRaises(SystemExit):
            sdc_qc.parse_mode("m1:SDC_MODE=x")

    def test_compress(self):
        self.assertEqual(sdc_qc.compress_names(["d[0]", "d[1]", "d[2]", "x", "d[5]"]),
                         ["x", "d[2:0]", "d[5]"])


if __name__ == "__main__":
    unittest.main()
