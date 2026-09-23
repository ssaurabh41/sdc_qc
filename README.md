# sdc_qc — SDC quality check against a gate-level netlist

`sdc_qc.py` checks SDC files delivered by another team before APR starts. It
reads a gate-level Verilog netlist (Design Compiler style, flat or with
sub-block netlists) and CCS/LVF Liberty files. It then runs each mode's SDC
in a real Tcl interpreter, with SDC and PrimeTime-style commands implemented
against that design. The report lists missing objects, broken clocks,
unconstrained I/O, suspicious exceptions, and differences between modes.

It is a **structural** checker. It does no delay calculation and reports no
slack. For signoff, PrimeTime or Tempus `check_timing` (and PrimeTime's
`report_exceptions -ignored`) remain the authority. `sdc_qc` is the fast,
licence-free check you run each time a new SDC arrives.

* Single file, standard library only: `sdc_qc.py`. It needs Python 3.6.2 or
  later with `tkinter`, which provides the Tcl interpreter; no Tk display is
  needed.
* `run.sh` is a tcsh wrapper with the run command and its usage in the header
  comments.
* `tests/` holds unit and regression tests, plus a small demo design with
  seeded SDC faults.

## Quick start

```tcsh
./run.sh                   # runs the demo in tests/data; edit SETTINGS in run.sh for your block
```

or directly:

```tcsh
python3 sdc_qc.py \
    -top foo -netlist foo.v \
    -lib /libs/std_tt.lib.gz /libs/sram_tt.lib.gz \
    -mode "mode1:SDC_MODE=mode1:foo.nondft.sdc" \
    -mode "mode2:SDC_MODE=mode2:foo.nondft.sdc:bar.sdc" \
    -mode "dft:foo.dft.sdc" \
    -jobs 8 -out_dir sdc_qc_out
```

Results are written to `sdc_qc_out/`:

| File | Content |
|---|---|
| `sdc_qc.rpt` | Readable report: summary by mode and severity, counts by rule, then every finding with `file:line`, the command and the objects |
| `sdc_qc.csv` | One row per finding: rule, severity, mode, file, line, command, objects, message |
| `sdc_qc.json` | Same findings plus a run summary (clock counts, clock-pin coverage, runtimes) |

Exit status: `0` means no ERROR findings, `1` means ERROR findings are present,
and `2` means a tool or usage error (missing file, bad option, internal error).

## Modes

```
-mode "<mode>:<VAR>=<value>:<file.sdc>[:<incremental.sdc>...]"
```

* Each `-mode` runs in a **fresh Tcl interpreter**, so variables, procs and
  clocks from one mode never leak into another. STA is done one mode at a time.
* The tokens are processed left to right. `VAR=value` sets a global Tcl
  variable, for example `SDC_MODE`, before the files that follow it are
  sourced. Files are sourced in the order given, so incremental SDCs go last.
* `source` inside an SDC is resolved relative to the sourcing file, then the
  `-search_path` directories, then the current directory. A missing file is
  reported as `PARSE-004` and the run continues.
* With `-jobs` > 1, modes run in parallel processes.

## Inputs

| Input | Notes |
|---|---|
| `-netlist` | One or more `.v` / `.v.gz` files. The top is auto-detected (override with `-top`). Sub-block netlists are elaborated into the hierarchy. |
| `-lib`, `-lib_list` | `.lib` / `.lib.gz`, CCS/LVF is fine. **One corner is enough**: only pin names, directions, bus types, `function`, `clock`, `ff`/`latch`/`statetable`, ICG attributes and each `timing()` group's `related_pin` / `timing_type` are read. |
| `-blackbox PATTERN` | A reference with neither a netlist module nor a Liberty cell is reported as `DES-002` ERROR. A matching `-blackbox` glob turns it into an INFO line; the block's pin names are taken from its instance connections. |

## What is checked

Each rule has a default severity; the few downgrades are noted in the table. Checks between modes are always WARNING.

| Rule | Severity | Check |
|---|---|---|
| PARSE-001 | ERROR | Tcl error while executing SDC (execution continues with the next command) |
| PARSE-002 | ERROR | Unknown command (includes the classic unbraced `x[0]` → Tcl command `0`) |
| PARSE-003 | ERROR | Unknown or malformed command option |
| PARSE-004 | ERROR | Sourced / mode SDC file not found |
| PARSE-005 | INFO | Tool command accepted but not modelled (`set_app_var`, `report_timing`, …) |
| PARSE-006 | WARNING | Unsupported attribute in `get_attribute` / `-filter` |
| PARSE-007 | WARNING | Deferred feature used (`all_fanin`/`all_fanout`) |
| DES-001 | ERROR | `current_design` does not match netlist top |
| DES-002 | ERROR | Unresolved cell reference (no module, no Liberty cell) |
| DES-003 | INFO | Reference black-boxed by `-blackbox` |
| DES-004 | WARNING | Netlist parse issue |
| DES-005 | WARNING | Instance pin not found on Liberty cell / module |
| LIB-001 | ERROR | Liberty read/parse problem |
| LIB-002 | WARNING | Cell defined in several libraries with different pins |
| UNIT-001 | ERROR | `set_units` does not match Liberty units |
| UNIT-002 | WARNING | Liberty files use different units |
| UNIT-003 | WARNING | Clock periods look implausible for the time unit (heuristic) |
| OBJ-001 | ERROR | Object query matched nothing (WARNING with `-quiet`) |
| OBJ-002 | ERROR | Constraint dropped: object option resolved to nothing |
| OBJ-003 | ERROR | Object of wrong type for option |
| CLK-001 | WARNING | Clock redefined (same name) |
| CLK-002 | WARNING | Clock source overwritten (another clock, no `-add`) |
| CLK-003 | ERROR | Invalid clock period |
| CLK-004 | ERROR | Invalid clock waveform |
| CLK-005 | WARNING | Questionable clock source object (output port; hierarchical pin = INFO) |
| CLK-006 | ERROR | Generated clock `-source` missing or unresolved |
| CLK-007 | ERROR | Generated clock `-master_clock` undefined |
| CLK-008 | WARNING | Generated clock master ambiguous / not found at `-source` |
| CLK-009 | ERROR | Invalid generated clock definition (`-divide_by`/`-multiply_by`/`-edges`) |
| CLK-010 | WARNING | Virtual clock never referenced |
| CLK-011 | ERROR | No clocks defined in mode |
| CLK-012 | INFO | `set_propagated_clock` in a pre-CTS SDC |
| COV-001 | WARNING | Register/macro clock pins not reached by any clock (grouped by where the trace stopped) |
| COV-002 | INFO | Clock trace stopped at logic that is not buffer/inverter/ICG (for example a clock mux) |
| COV-003 | WARNING | Register/macro clock pin tied constant or undriven |
| IO-001 | WARNING | Input port without `set_input_delay` |
| IO-002 | WARNING | Output port without `set_output_delay` |
| IO-003 | ERROR | I/O constraint on port of wrong direction |
| IO-004 | ERROR | I/O delay without a valid `-clock` |
| IO-005 | WARNING | I/O delay ≥ `-io_threshold` (default 80 %) of the clock period |
| IO-006 | WARNING | I/O delay has `-max` but no `-min` (or vice versa) |
| IO-007 | WARNING | Input port without driving cell / drive / input transition |
| IO-008 | WARNING | Output port without `set_load` |
| EXC-001 | WARNING | `-from` object is not a valid timing startpoint (INFO for `set_max/min_delay`: path segmentation) |
| EXC-002 | WARNING | `-to` object is not a valid timing endpoint (same) |
| EXC-003 | WARNING | Setup multicycle without matching hold multicycle |
| EXC-004 | WARNING | Duplicate / overlapping timing exception |
| EXC-005 | INFO | Exception redundant with `set_clock_groups` |
| EXC-006 | WARNING | Exception without `-from/-to/-through` (applies to all paths) |
| CG-001 | ERROR | Clock in more than one group of a `set_clock_groups` |
| CG-002 | INFO | `set_clock_groups` with a single `-group` |
| CASE-001 | ERROR | Invalid `set_case_analysis` value |
| CASE-002 | WARNING | Conflicting `set_case_analysis` on the same object |
| MISC-001 | ERROR | Invalid pin name in `set_disable_timing -from/-to` |
| MISC-002 | WARNING | Unsupported `set_hierarchy_separator` |
| MODE-001 | WARNING | Clock defined in some modes only |
| MODE-002 | WARNING | Same clock name differs across modes (period/waveform/sources) |
| MODE-003 | WARNING | Port I/O-delay coverage differs across modes |
| MODE-004 | WARNING | Port drive/load coverage differs across modes |

Startpoints, endpoints and clock pins come **only from the Liberty timing
arcs**, never from pin names:

* A clock pin is the `related_pin` of a `setup_*`/`hold_*`/`recovery_*`/`removal_*`
  or `rising_edge`/`falling_edge` arc, or a pin with `clock : true`, or a pin in an
  `ff clocked_on` / `latch enable` expression.
* An endpoint is a pin that owns a constraint arc.
* A buffer or inverter is identified from its `function`.
* An ICG is identified from `clock_gating_integrated_cell`.

SRAMs and hard macros therefore get the same treatment as flip-flops.

### SDC and Tcl support

* **Object queries:**
  * `get_ports/pins/cells/nets/clocks/lib_cells/lib_pins/libs`, with
    `-hierarchical`, `-regexp`, `-nocase`, `-quiet`, `-filter`, `-of_objects`
    and `-leaf`.
  * `all_inputs/outputs/clocks/registers`; `all_registers -clock` uses the clock trace.
  * Collections: `foreach_in_collection`, `sizeof_collection`,
    `get_object_name`, `add/remove/append_to_collection`, `filter_collection`,
    `index_collection` and `get_attribute`.
  * `define_proc_attributes` / `parse_proc_arguments`, `redirect`,
    `current_instance`.
  * `-filter`: `== != =~ !~ < > <= >=`, `&& ||` (and `and`/`or`), `!`,
    parentheses, `defined()`/`undefined()`, and a leading `@`.
* **Patterns:**
  * `*` and `?` do not cross `/`. `[` `]` are literal bus brackets, and a bus
    base name matches all its bits.
  * `-hierarchical` matches the pattern against every trailing part of the
    full name that starts after a `/`.
* **Sandbox:** the SDC comes from another team, so `exec`, `socket`, `cd`,
  `load`, `exit`, file writes and destructive `file` subcommands are disabled.
  `puts` is silenced.
* **Line numbers:** Tcl `info frame` gives the exact line for commands at the
  top level of a file, inside `if`/`foreach`/`for` bodies and inside called
  procs (reported at the call site). Inside a `foreach_in_collection` body, the
  finding points at the `foreach_in_collection` line.

## Performance

Measured on a 4-core VM with a synthetic flat DC-style netlist of 2,000,065
instances (180 MB) and four CCS-like libraries of 487 MB each:

| Stage | `-jobs 1` | `-jobs 4` |
|---|---|---|
| Liberty, first run, 4 × 487 MB | ≈ 9 s (estimated from 2.2 s per file, ≈220 MB/s) | 1.6–2.4 s (measured) |
| Liberty, cached | < 0.1 s | < 0.1 s |
| Netlist read + elaboration + driver map | 23 s | 12 s |
| One mode (2,000 constraints, wildcard/`-hier` queries, 500k clock pins traced) | 3.2 s | 3.2 s (modes run in parallel) |
| **Total wall (libraries cached)** | **27 s** | **16 s** |

Peak RSS was 1.1 GB for 2M instances.

How it gets there:

* **Liberty:** only the needed groups are interpreted. All table groups
  (CCS vectors, LVF sigma, NLDM, power, noise) are skipped by brace counting
  with `bytes.find`/`bytes.count`, so table values are never tokenized.
  `.gz` files are decompressed by `pigz`/`gzip` in a separate process.
  Libraries are parsed in parallel **processes**; the Python GIL makes threads
  useless for this CPU-bound work. Each extract is cached in `-cache_dir`
  (default `~/.cache/sdc_qc`), keyed by path + size + mtime + extractor
  version, so later QC runs skip parsing.
* **Why not `.db`?** It is a proprietary Synopsys binary format with no public
  specification or supported Python reader. Reading it needs a licensed
  Synopsys tool, which defeats a licence-free pre-check. The extract cache
  gives a faster reload than `.db` would.
* **Netlist:**
  * Declarations are found with `str.find`.
  * Bodies larger than 16 MB are split on `;` boundaries and parsed by forked
    worker processes.
  * Connectivity is stored in flat `array`s: one net id per cell pin, and one
    union-find for `assign` and hierarchy aliases.
* **Queries:** exact names are dict lookups. Wildcards use a C-speed substring
  prefilter on a newline-joined name blob, then a regex on candidate lines.
  Results are cached per pattern. The net→pin index (for `-of_objects` on
  nets) is built only on first use.
* **Clock trace:** a backward walk from each register or macro clock pin
  through buffers, inverters and ICGs, memoized per net.

## Assumptions and known limitations

These are deliberate simplifications. Challenge any that do not match your flow.

1. **Flat escaped names.** `\u_core/U45 ` is treated like a hierarchical name
   for pattern matching, so `get_cells u_core/*` matches it. `get_cells *`
   (non-hierarchical) does not. This is how PrimeTime is commonly used on
   flat DC netlists, but it has not been checked against your PrimeTime
   version.
2. **Implicit name lookup order** in `-from/-to` is clock, then port, then
   pin, then cell. `-through` is port, pin, cell, net. A name that is both a
   clock and a port resolves to the clock.
3. **`all_registers`** excludes ICG cells and includes macros that have
   sequential timing arcs (for example SRAMs).
4. **Clock trace** passes only through buffers, inverters and ICGs. It stops at
   muxes and other logic (COV-002 INFO); it does not propagate through them.
   Generated clocks count as sources where they are defined.
5. **An exception is ignored** when all of its `-from`/`-to` objects are invalid.
   STA tools warn about this; confirm with `report_exceptions -ignored` in
   PrimeTime.
6. **Unbalanced braces** inside Liberty comments or strings are not
   supported. The result is `LIB-001 unbalanced braces`.
7. **Escaped Verilog identifiers** that contain `;`, `(` or `)` are not
   supported.
8. **Python 3.6.2:** static analysis (`vermin`) reports that nothing newer
   than Python 3.5 is required. The tests were run on **3.9.7 (Tcl 8.6.10)**.
   A real 3.6 interpreter was not available in the development sandbox.
   RHEL 7 ships Tcl 8.5 with its Python 3.6; every Tcl feature used exists in
   8.5, but that combination is untested.

## Deferred to later releases

* `set_driving_cell -lib_cell` existence check.
* Checking that a generated clock's `-source` is actually reached by its master
  through the netlist. Today only a master defined directly on `-source` is
  checked, and anything else is reported as CLK-008 INFO. The backward clock
  trace used by COV-001 is the natural base for this.
* `all_fanin` / `all_fanout`. They return an "unknown" collection with a
  PARSE-007 warning, and object checks on it are skipped.
* Clock propagation through muxes and other logic.

## Tests

```tcsh
python3 tests/test_sdc_qc.py -v
```

27 tests cover:

* Liberty extraction, including CCS/LVF groups, `test_cell`, buses,
  multi-name pins, gzip, a 7-byte read chunk that exercises every
  buffer-boundary case, and the cache.
* Netlist elaboration: hierarchy, concatenations, bus pins, `assign`,
  constants, black boxes.
* Pattern and filter semantics.
* Line numbers inside loops and procs.
* Error recovery and the sandbox.
* Generated-clock and unit checks.
* Mode and variable sequencing, and every seeded fault in the demo SDCs.
* Equality of the serial and parallel paths, for both netlist elaboration
  and modes.
