# sdc_qc investigation findings — review draft

Date: 2026-09-23. Source: `ssaurabh41/sdc_qc`, commit `b1cd937003a4daa97fff6fa0fb052e4e09a36af1`, version 1.0.0.

Status: five findings accepted by the coordinating review and approved by the user for a findings-only commit on `codex/findings-1`. No checker fixes are included.

## Outcome and scope

The checker works on the ordinary explicit-instance designs tested: object queries, clock coverage, hierarchy, multi-mode isolation, incremental SDC sourcing, and many injected fault rules matched independent expectations. It is not reliable for Verilog instance arrays, and its I/O constraint bookkeeping has two gaps. Windows has two reproducible invocation/elaboration failures.

Two Flash agents independently authored designs in separate source snapshots. Astra reviewed the reports, rejected unsupported claims, inspected relevant source, and independently reproduced all five accepted findings against the original checkout. The source SHA-256 remained `777fbb805c0b73624576dfc1e0df019681169bfe0d1756ebdf2d219298b78c4a`.

Environment: Windows 10.0.19045, Python 3.14.7, Tcl 9.0.4, real `tkinter.Tcl()`, no reference STA. Results establish observed behavior and consistency with this project's contracts, not PrimeTime/Tempus equivalence. The README's tested Python 3.9.7/Tcl 8.6.10 and Linux fork paths were not exercised here.

## Test evidence

| Area | Evidence |
|---|---|
| Small designs | Independent 9–25-cell fixtures; 80 semantic cases after review (77 original plus 3 controls) and 13 CLI checks; all 59 rule IDs exercised at least once. |
| Scaling and hierarchy | 41 case scenarios; clean flat/hier pairs with 133/141, 4,087/4,148 and 32,717/33,187 cells. Exact clock-pin coverage: 64/64, 1,990/1,990 and 15,896/15,896 respectively. |
| Fault injections | SDC mutation case produced 13 expected findings. Four changed clock connections produced exactly the expected constant, undriven, mux-stop and missing-clock findings and reduced coverage from 1,990 to 1,986. |
| Equivalence checks | 13 structural/coverage invariants held. Selected `-jobs 1` and `-jobs 4` results matched below the large-module threshold; mode execution remains serial on Windows. |
| Long chain | 100,008 instances, 100,000 buffer/inverter stages and 8 register clock pins: serial run clean, 8/8 coverage, about 3.3 s. Default/multiple jobs failed on Windows. |
| Larger parser stress | Separate 520,000-cell combinational design: serial run clean, about 7–10 s. This design has no register clock pins and does not establish sequential coverage at that size. |
| Supplied regression suite | 28 tests executed after a sandbox-only temporary-directory workaround: 25 passed, 2 failed and 1 errored. Failures are consequences of findings 4 and 5 below, not additional defects. |
| Supplied benchmark | Relative SDC path: 544 cells, 181/181 clock pins, zero findings. Absolute Windows SDC path: spurious ERROR. |
| Independent root review | One-register controls, four-instance array versus expanded form, dropped I/O delay, pin targets, absolute paths and a module-body threshold probe; outputs retained separately. |

The agents' “80/80” and “41/41 as recorded” numbers mean observation signatures reproduced, including known failures and expected warnings. They do **not** mean that the checker passed 121 independent correctness assertions. Some expectations were refined during investigation. The concrete controls, raw outputs and findings below are the evidence for acceptance.

## 1. P1 — Instance arrays are silently collapsed, producing false complete coverage

Source: `sdc_qc.py:1008`, `sdc_qc.py:1275` (`Elaborator._inst_stmt`).

The fast parser detects an instance range and sends it to the slow path. The slow path appends the range text to a single instance name instead of expanding the array. Both Liberty-cell arrays and module arrays are affected.

Independent minimal input:

```verilog
module top (clk,d,q);
  input clk,d;
  output q;
  FF u[3:0] (.CK(clk), .D(d), .Q());
  assign q=d;
endmodule
```

The SDC defines a clock on `clk` and constrains all data ports. The Liberty describes an ordinary FF with CK, D and Q.

- Expected: four FF instances and four reached clock pins, or an explicit unsupported-array diagnostic preventing a misleading clean result.
- Actual: one cell named `u[3:0]`, coverage **1/1**, zero findings, exit 0.
- Control: four explicitly named instances with identical connections produce **4 cells, 4/4 coverage**, zero findings.
- A second independent fixture containing cell and module arrays produces 3 cells/2 clock pins instead of 8/6. It emits only a connection-width warning, not an array-support diagnostic.

Impact: missing instances disappear from both queries and the denominator of the coverage metric. This is the most consequential observed false-clean result. Although the project targets DC-style gate netlists, arrays are not identified as an unsupported input form in the README; silent under-elaboration should be prevented even if full support remains out of scope.

Suggested acceptance criterion: correctly expand arrays and distribute connections, or reject/report unsupported arrays before presenting full coverage. Cover both cell and module arrays and retain explicit-instance controls.

## 2. P2 — A constraint reported as dropped still satisfies I/O coverage

Source: `sdc_qc.py:3093` (`SdcEngine._io`), `sdc_qc.py:3117`, `sdc_qc.py:3511` (`ModeChecker.check_io`).

`_io` does not check `rec["dropped"]`; it stores a record even when the `-clock` object failed resolution. The later coverage/min/max pass consumes that record as if the constraint had been applied.

```tcl
create_clock -name C -period 2 [get_ports clk]
set_input_delay -clock MISSING 0.2 [get_ports d]
```

- Control without an input delay: `IO-001` reports `d`.
- Faulty constraint above: `OBJ-001` for MISSING and `OBJ-002` saying the constraint was dropped; **IO-001 for d disappears**.
- When a valid min/max pair already exists, a dropped `-max` record can instead add a misleading IO-006 warning in a separate unresolved-clock bucket.

Impact: inaccurate coverage and confusing secondary findings. The overall faulty run still exits 1 because object errors are present; this is **not** a false-clean exit.

Suggested acceptance criterion: dropped records must not enter applied I/O coverage or min/max state. Keep the unresolved-clock errors and report the genuinely unconstrained port.

## 3. P2 — Pin-targeted I/O delays are accepted and silently discarded

Source: `sdc_qc.py:1815`, `sdc_qc.py:1819` (command signatures), `sdc_qc.py:3104` (`_io`).

The command signatures permit `port,pin` targets, but `_io` processes only `port` keys. Pin targets are neither modeled nor diagnosed.

Append either of these to the fully constrained clean single-FF design:

```tcl
set_input_delay  -clock C 0.3 [get_pins u/D]
set_output_delay -clock C 0.3 [get_pins u/Q]
```

Actual for either command: zero findings, exit 0. Source inspection confirms that the associated I/O record has no target ports. A cell target in the same position does produce an object-type diagnostic, so this is specifically the permitted-but-unhandled pin path.

Impact: a delivered constraint can be ignored without alerting the QC reviewer. With otherwise missing port constraints, generic coverage warnings may appear, but they do not identify this ignored command.

Suggested acceptance criterion: implement the supported pin semantics or explicitly diagnose unsupported pin targets. The finding does not prescribe OBJ-003 for all pins or claim that all pin-based I/O constraints are illegal.

## 4. P2 — Absolute Windows paths break the `-mode` grammar

Source: `sdc_qc.py:3788`, especially `spec.split(":")` at line 3790.

```powershell
python sdc_qc.py -netlist tests/data/bench.v -lib tests/data/cells.lib -mode "func:D:\path\to\sdc_qc\tests\data\bench.sdc" -jobs 1 -no_cache
```

The drive letter becomes a separate SDC filename. On the same drive, the remaining rooted path may still source the intended SDC, but the tool emits `PARSE-004: mode SDC file not found: D` and exits 1. The exact same clean benchmark with `-mode "func:tests/data/bench.sdc"` exits 0 with 181/181 coverage.

Impact: clean input fails QC through a normal Windows path representation. If the current drive differs, the remaining path can also resolve incorrectly. Two supplied tests fail because their helper builds absolute mode paths. Values containing colons also need a deliberate grammar/escaping decision.

Workaround: use relative SDC paths within `-mode`. Suggested acceptance criterion: accept supported absolute path forms while preserving ordered mode variables and incremental SDCs, or clearly restrict platforms/path syntax.

## 5. P2 — Large module elaboration unconditionally requests `fork` on Windows

Source: `sdc_qc.py:965` (16 MiB threshold), `sdc_qc.py:1216`, `sdc_qc.py:1220`.

When a module body exceeds 16 MiB and jobs exceed one, `_elab_module` calls `multiprocessing.get_context("fork")` unconditionally. Windows lacks that context. The separate mode-parallel path already guards this condition at `sdc_qc.py:3925`.

- 100,008-instance netlist, approximately 22.9 MB: default jobs and `-jobs 4` both exit **2**, traceback ends in `ValueError: cannot find context for 'fork'`, no report.
- Same design with `-jobs 1`: clean, 8/8 register clock pins reached.
- Independent 520,000-cell netlist and root module-size probe reproduce the same result.
- The default is `min(8, cpu_count)`, so an ordinary default invocation is affected on multicore Windows machines.

Suggested acceptance criterion: fall back to serial elaboration on platforms without fork, or implement a portable multiprocessing context with proper state transfer. Test the body-size boundary and default invocation, not just explicit `-jobs` arguments.

## Claims excluded or qualified during review

- **Register-Q `-from` “false positive”: not accepted.** No PrimeTime run substantiated the worker's initial claim. Primary [AMD set_false_path documentation](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/set_false_path) and [OpenSTA command documentation](https://opensta.readthedocs.io/en/latest/Commands/#set_false_path) describe register clock pins as valid startpoints. The observed Q-pin warning alone does not establish a checker bug.
- **Cross-clock IO-006 “false positive”: not accepted.** The proposed case supplies a min bound for one clock and only a max bound for another. That does not establish a matched explicit pair for the second clock. It also omits `-add_delay`, which affects replacement semantics. A legal SDC may intentionally receive this heuristic warning; no per-port-only fix is recommended. [OpenSTA set_input_delay documentation](https://opensta.readthedocs.io/en/latest/Commands/#set_input_delay).
- **Process-pool permission error:** the sandbox blocks Windows named pipes even in a standalone ProcessPoolExecutor probe. This is an environment limitation, not another demonstrated checker defect. Serial fallback would be an optional robustness improvement.
- **Temporary-directory permissions:** the supplied suite initially failed before assertions because sandbox-created `tempfile.mkdtemp` directories were unwritable. Replacing only the harness temporary-directory helper allowed the unchanged 28 tests to run. The two failures/one error then remaining were independently reproduced via CLI.
- **Documented limits:** mux clock propagation, generated-clock source reachability and deferred all_fanin/all_fanout were not counted as new defects. No Linux fork, commercial STA, production Liberty corner or `.db` validation was performed.

## Evidence locations and review handoff

The full local evidence is under ignored `sdc_qc_out/investigation-1/`:

- `workspaces/small/REPORT.md`, `harness/`, `repros/`, `results/`.
- `workspaces/scale/inv1/REPORT.md`, `gen/`, `fixtures/`, `results/`.
- `review/reproduce.py`: coordinator's self-contained reproducer below.
- Independent coordinator outputs: `sdc_qc_out/findings1-review-da5ffa96/review.json` and its per-case subdirectories.

These large local fixtures are not part of this findings-only commit. The embedded script below recreates all five accepted behaviors using only the repository's checker, Python and tkinter. Save the script as `reproduce_findings1.py` in the repository root and run it from there; it creates a unique directory beneath `sdc_qc_out/`. `--large` adds a 17 MiB whitespace prefix inside an otherwise identical module to isolate the multiprocessing threshold without generating a large circuit. Large realistic circuit evidence is separate, as described above.

Expected on the reviewed base: clean 1/1; array incorrectly 1/1; expanded 4/4; missing_input IO-001; dropped_input OBJ-001/OBJ-002 but no IO-001; pin_input/pin_output no findings; absolute_mode PARSE-004 on Windows; large_serial clean and large_default exit 2 on Windows with multiple default jobs.

```text
python reproduce_findings1.py --large
```

This is an observation/reproduction script, not a replacement for independent regression assertions. Preserve the base revision while evaluating it.

```python
"""Run from the repository root: python <path-to-this-script> [--large]."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

repo = Path.cwd().resolve()
checker = repo / 'sdc_qc.py'
assert checker.is_file(), 'Run from the sdc_qc repository root'
root = repo / 'sdc_qc_out' / ('findings1-review-' + uuid.uuid4().hex[:8])
root.mkdir(parents=True)
lib = '''library (mini) {
  time_unit : "1ns";
  capacitive_load_unit (1,pf);
  cell (FF) {
    ff (IQ,IQN) { next_state : "D"; clocked_on : "CK"; }
    pin (CK) { direction : input; clock : true; }
    pin (D) { direction : input;
      timing () { related_pin : "CK"; timing_type : setup_rising; }
      timing () { related_pin : "CK"; timing_type : hold_rising; }
    }
    pin (Q) { direction : output; function : "IQ";
      timing () { related_pin : "CK"; timing_type : rising_edge; }
    }
  }
}
'''
header = 'module top (clk,d,q); input clk,d; output q;\n'
body = 'FF u (.CK(clk), .D(d), .Q(q));\n'
netlist = header + body + 'endmodule\n'
sdc = '''create_clock -name C -period 2 [get_ports clk]
set_input_delay -clock C 0.2 [get_ports d]
set_output_delay -clock C 0.2 [get_ports q]
set_input_transition 0.1 [all_inputs]
set_load 0.01 [all_outputs]
'''
rows = []

def run(name, v, constraints, jobs='1', absolute_mode=False):
    work = root / name
    work.mkdir()
    (work / 'mini.lib').write_text(lib)
    (work / 'top.v').write_text(v)
    (work / 'case.sdc').write_text(constraints)
    mode_path = str(work / 'case.sdc') if absolute_mode else 'case.sdc'
    cmd = [sys.executable, str(checker), '-netlist', 'top.v', '-lib', 'mini.lib',
           '-mode', 'm:' + mode_path, '-no_cache', '-out_dir', 'out']
    if jobs is not None:
        cmd += ['-jobs', jobs]
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=120)
    (work / 'stdout.txt').write_text(proc.stdout)
    (work / 'stderr.txt').write_text(proc.stderr)
    report = work / 'out' / 'sdc_qc.json'
    data = json.loads(report.read_text()) if report.is_file() else {}
    summary = data.get('summary', {})
    row = dict(name=name, command=cmd, exit=proc.returncode,
               seconds=round(time.perf_counter()-started, 3),
               cells=summary.get('cells'), modes=summary.get('modes'),
               findings=data.get('findings'), stderr=proc.stderr)
    rows.append(row)
    print(name, 'exit=', row['exit'], 'cells=', row['cells'],
          'coverage=', [m.get('coverage') for m in row['modes'] or []],
          'findings=', [(f['rule'], f['obj']) for f in row['findings'] or []])
    return row

run('clean', netlist, sdc)
run('array', header + 'FF u[3:0] (.CK(clk), .D(d), .Q());\nassign q=d;\nendmodule\n', sdc)
run('expanded', header + ''.join('FF u%d (.CK(clk), .D(d), .Q());\n' % i for i in range(4))
    + 'assign q=d;\nendmodule\n', sdc)
run('missing_input', netlist, sdc.replace('set_input_delay -clock C 0.2 [get_ports d]\n', ''))
run('dropped_input', netlist, sdc.replace('set_input_delay -clock C', 'set_input_delay -clock MISSING'))
run('pin_input', netlist, sdc + 'set_input_delay -clock C 0.3 [get_pins u/D]\n')
run('pin_output', netlist, sdc + 'set_output_delay -clock C 0.3 [get_pins u/Q]\n')
if os.name == 'nt':
    run('absolute_mode', netlist, sdc, absolute_mode=True)
if '--large' in sys.argv:
    # Preserve exactly one register while exceeding the 16 MiB module-body trigger.
    padded = header + (' ' * (17 * 1024 * 1024)) + body + 'endmodule\n'
    run('large_serial', padded, sdc)
    run('large_default', padded, sdc, jobs=None)
record = dict(checker_sha256=hashlib.sha256(checker.read_bytes()).hexdigest(),
              python=sys.version, rows=rows)
(root / 'review.json').write_text(json.dumps(record, indent=2))
print('Evidence:', root)

```
