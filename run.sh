#!/bin/tcsh -f
#==============================================================================
# run.sh - run the SDC QC checker (sdc_qc.py)
#
# USAGE
#   ./run.sh                 run with the settings in the SETTINGS block below
#   ./run.sh <extra opts>    extra options are passed to sdc_qc.py, e.g.
#                              ./run.sh -jobs 16 -no_clock_trace
#
#   Out of the box the SETTINGS point at the small demo design in tests/data,
#   so "./run.sh" works immediately. Edit the SETTINGS for your block.
#
# SETTINGS
#   PYTHON    python3 (3.6.2+ with tkinter)
#   TOP       netlist top module (leave "" to auto-detect)
#   NETLISTS  gate-level netlist(s): the top, plus sub-block netlists if the
#             top instantiates them (.v or .v.gz)
#   LIBS      Liberty files (.lib or .lib.gz), CCS/LVF is fine. One corner
#             is enough: only pins, directions and timing-arc types are read.
#   LIB_LIST  alternatively, a text file with one Liberty path per line
#   BLACKBOX  reference names/patterns that have neither netlist nor .lib;
#             they become black boxes instead of DES-002 errors
#   MODES     one entry per STA mode (each runs in a fresh Tcl interpreter):
#               "<mode>:<VAR>=<value>:<file.sdc>[:<incremental.sdc>...]"
#             VAR=value is set as a global Tcl variable BEFORE the files that
#             follow it; files are sourced left to right. Examples:
#               "mode1:SDC_MODE=mode1:foo.nondft.sdc"
#               "mode2:SDC_MODE=mode2:foo.nondft.sdc:bar.sdc"
#               "dft:foo.dft.sdc"
#   OUT_DIR   report directory (sdc_qc.rpt, .csv, .json and the sdc_qc.html dashboard)
#   JOBS      parallel processes (Liberty parsing, netlist, modes)
#
# EXIT STATUS
#   0 = no unwaived ERROR findings, 1 = unwaived ERROR findings present,
#   2 = tool/usage error. Waivers: ./run.sh -waivers blk.sdc_qc.waivers.json
#
# All options: python3 sdc_qc.py -h      Documentation: README.md
#==============================================================================

set script_dir = `dirname $0`
set script_dir = `cd $script_dir && pwd`
set demo = $script_dir/tests/data

#------------------------------- SETTINGS -------------------------------------
set PYTHON   = python3
set TOP      = "foo"
set NETLISTS = ( $demo/foo.v $demo/sub.v )
set LIBS     = ( $demo/cells.lib )
set LIB_LIST = ""
set BLACKBOX = ( hardip )
set MODES    = ( \
    "mode1:SDC_MODE=mode1:$demo/foo.nondft.sdc:$demo/bar.sdc" \
    "mode2:SDC_MODE=mode2:$demo/foo.nondft.sdc" \
    "dft:$demo/foo.dft.sdc" \
)
set OUT_DIR  = sdc_qc_out
set JOBS     = 8
#------------------------------------------------------------------------------

set args = ( -netlist $NETLISTS -out_dir $OUT_DIR -jobs $JOBS )
if ( "$TOP" != "" ) set args = ( $args -top $TOP )
if ( $#LIBS > 0 ) set args = ( $args -lib $LIBS )
if ( "$LIB_LIST" != "" ) set args = ( $args -lib_list $LIB_LIST )
foreach bb ( $BLACKBOX )
    set args = ( $args -blackbox "$bb" )
end
foreach m ( $MODES:q )
    set args = ( $args:q -mode "$m" )
end

echo "sdc_qc: $PYTHON $script_dir/sdc_qc.py $args:q $argv:q"
$PYTHON $script_dir/sdc_qc.py $args:q $argv:q
set rc = $status
echo "sdc_qc: exit status $rc (0 clean, 1 unwaived ERROR findings, 2 tool error); report: $OUT_DIR/sdc_qc.rpt, dashboard: $OUT_DIR/sdc_qc.html"
exit $rc
