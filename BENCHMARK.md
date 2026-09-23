# Running sdc_qc on the DATC First-Commercial-Benchmarking-SiemensEDA set

This benchmark (IEEE CEDA DATC) ships real gate-level netlists, SDC and
Liberty for several open designs across four technologies (asap7, ng45,
sky130hd, ihp130). It is a good real-design smoke test for `sdc_qc.py`,
distinct from the small seeded-fault demo in `tests/data`.

## 1. Clone the benchmark

```tcsh
git clone https://github.com/ieee-ceda-datc/First-Commercial-Benchmarking-SiemensEDA.git bench
```

It is large (~1.4 GB after clone). Layout used below:

```
bench/datain/<tech>/Netlist/<design>.v
bench/datain/<tech>/SDC/<design>.sdc
bench/datain/<tech>/LIB/*.lib
```

`<tech>` is one of `asap7`, `ng45`, `sky130hd`, `ihp130`. Designs per tech:

| tech | designs |
|---|---|
| asap7, ng45 | `aes_cipher_top`, `jpeg_encoder`, `ariane`, `bsg_chip` |
| sky130hd, ihp130 | `aes_cipher_top`, `ibex_core` |

Netlist/SDC filenames include the tech suffix for asap7/ng45 (e.g.
`aes_cipher_top_asap7.v`) but not for sky130hd/ihp130 (e.g. `aes_cipher_top.v`).

## 2. Run sdc_qc on one design

```tcsh
python3 sdc_qc.py \
    -top aes_cipher_top \
    -netlist bench/datain/asap7/Netlist/aes_cipher_top_asap7.v \
    -lib bench/datain/asap7/LIB/*.lib \
    -mode "func:bench/datain/asap7/SDC/aes_cipher_top_asap7.sdc" \
    -out_dir qc_out/asap7_aes_cipher_top \
    -jobs 4
```

Adjust `-top`, `-netlist`, `-lib` and the SDC path per the table above.
`bsg_chip` is the largest design (~800K-1M cells) and needs a few GB of RAM
and ~30-40s; the rest run in a few seconds.

## 3. Run it on every design/tech pair

```tcsh
#!/bin/tcsh -f
set B = bench/datain
foreach t (asap7 ng45)
  foreach d (aes_cipher_top jpeg_encoder ariane bsg_chip)
    python3 sdc_qc.py -top $d \
        -netlist $B/$t/Netlist/${d}_$t.v \
        -lib $B/$t/LIB/*.lib \
        -mode "func:$B/$t/SDC/${d}_$t.sdc" \
        -out_dir qc_out/${t}_${d} -jobs 4
  end
end
foreach t (sky130hd ihp130)
  foreach d (aes_cipher_top ibex_core)
    python3 sdc_qc.py -top $d \
        -netlist $B/$t/Netlist/$d.v \
        -lib $B/$t/LIB/*.lib \
        -mode "func:$B/$t/SDC/$d.sdc" \
        -out_dir qc_out/${t}_${d} -jobs 4
  end
end
```

Each design writes `qc_out/<tech>_<design>/sdc_qc.rpt` (plus `.csv`/`.json`).

## 4. What to expect

These are clean, tool-generated SDCs (Innovus-derived), so `sdc_qc` finds
0 ERRORs on all 12 pairs. Typical WARNINGs are legitimate and expected for
this benchmark, not bugs in the design or the tool:

- **IO-001/002/007/008** — most of these SDCs only have `create_clock`; there
  are no `set_input_delay`/`set_output_delay`/drive/load constraints.
- **IO-006** (ihp130 `ibex_core`) — I/O delays given as `-max` only, no `-min`.
- **COV-002** (ibex_core on sky130hd/ihp130) — the clock-gate cell is a plain
  AND2 gate, not a Liberty-tagged integrated clock gating (ICG) cell, so the
  clock trace stops there (informational).
- **UNIT-002** (asap7, ng45) — the multi-file Liberty sets for these two
  techs mix time/capacitance units across files (e.g. std cells in ps,
  SRAMs in ns); `sdc_qc` reports the mismatch and uses the first library's
  units for its own unit-based checks (matching how PrimeTime/Innovus pick
  units for a multi-lib link).

If you see an ERROR, or a WARNING count that looks implausibly large (in the
thousands, roughly matching the sequential cell count), it is worth checking
whether it is a genuine SDC issue or a `sdc_qc` false positive — file an
issue with the `sdc_qc.rpt`, the specific rule id, and which
`<tech>_<design>` pair triggered it.
