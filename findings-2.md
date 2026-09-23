# Findings 2: Verification of latest main

Baseline: `cb2da32030fe3fb400a0d6f98611c1a9eea0b745` (`origin/main`, 2026-09-23). Verification used the official Python 3.6.2 x64 runtime and Tcl 8.6.6, as specified by the repository README.

## Previously reported issues are fixed

All five findings documented in `findings-1.md` have regression coverage and pass on this baseline:

1. Verilog instance arrays expand.
2. Dropped I/O delays do not count toward coverage.
3. Pin-targeted I/O delays produce IO-009.
4. Windows drive paths parse correctly in `-mode`.
5. Large-module elaboration falls back to serial execution on Windows without `fork`.

The supplied suite passed: `Ran 33 tests in 0.628s — OK`. The clean benchmark also exited 0 with 544 cells and 181/181 clock-pin coverage.

## Additional findings

### S1 — Tcl sandbox can be bypassed through exposed command aliases (High)

In `sdc_qc.py` around lines 1953, 1958, and 1965, Tcl built-ins are renamed to implementation aliases, but those aliases remain callable from SDC. The guarded `open` and `file delete` commands reject unsafe operations, but direct alias calls bypass those checks.

Using Python 3.6.2, a probe wrote a file with `__qc_tcl_open` and `__qc_tcl_puts`, then deleted a seeded file with `__qc_tcl_file delete`. The normal wrapper forms rejected the equivalent operations. This violates the sandbox contract that disables file writes and destructive file operations, and is a security boundary issue for untrusted SDC input.

**Suggested fix:** prevent SDC from invoking the underlying aliases, or apply the same restrictions at the underlying command boundary. Add regression tests for direct alias write and delete attempts.

### S2 — Non-finite timing values are accepted (Medium)

`_num` in `sdc_qc.py` around line 2049 converts strings with `float()` without rejecting non-finite values. Under Python 3.6.2, `create_clock -name C -period NaN [get_ports clk_a]` exits successfully without CLK-003; `Inf` is also accepted. Zero, negative values, and `-Inf` do produce CLK-003.

NaN and infinities are not valid timing periods or delays and can undermine downstream calculations and diagnostics.

**Suggested fix:** reject NaN and positive/negative infinity when parsing timing and unit values, with an appropriate diagnostic. Extend coverage to delays, waveforms, uncertainty, and exceptions.

## Scope and evidence limits

There was no commercial PrimeTime/Tempus comparison. A broader delegated semantic sweep did not complete because of a provider rate limit; findings here are limited to the completed regression run and targeted probes. Probe scripts and detailed output are preserved locally under the ignored `sdc_qc_out/investigation-2/` directory.
