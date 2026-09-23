@echo off
rem ===========================================================================
rem run.bat - run the SDC QC checker (sdc_qc.py) on Windows
rem
rem USAGE (from cmd.exe or PowerShell, in any directory)
rem   run.bat                   run with the SETTINGS below
rem   run.bat -jobs 4           extra options are passed to sdc_qc.py
rem
rem   Out of the box the SETTINGS point at the demo design in tests\data, so
rem   "run.bat" works immediately. Edit the SETTINGS for your block.
rem
rem REQUIREMENTS
rem   Python 3.6.2+ from python.org (includes tkinter; keep "tcl/tk and IDLE"
rem   ticked in the installer). Check with:  py -3 -c "import tkinter"
rem
rem SETTINGS
rem   PYTHON    Python launcher: "py -3" (python.org default) or "python"
rem   TOP       netlist top module (empty = auto-detect)
rem   NETLISTS  space-separated netlist files (.v / .v.gz)
rem   LIBS      space-separated Liberty files (.lib / .lib.gz); one corner is enough
rem   BLACKBOX  "-blackbox <pattern>" entries for references with no netlist/.lib
rem   MODES     one  -mode "<mode>:<VAR>=<value>:<file.sdc>[:<incr.sdc>...]"  per
rem             STA mode. VAR=value is set before the files that follow it;
rem             files are sourced left to right. Drive letters are fine:
rem               -mode "func:D:\proj\sdc\foo.sdc"
rem   OUT_DIR   report directory (sdc_qc.rpt, .csv, .json and the sdc_qc.html dashboard)
rem   JOBS      parallel processes (Liberty parsing; netlist and modes run
rem             serially on Windows)
rem   Paths containing spaces must be quoted, e.g. "C:\my libs\std.lib".
rem
rem EXIT STATUS
rem   0 = no unwaived ERROR findings, 1 = unwaived ERROR findings present,
rem   2 = tool/usage error. Waivers: run.bat -waivers blk.sdc_qc.waivers.json
rem
rem All options: py -3 sdc_qc.py -h      Documentation: README.md
rem ===========================================================================
setlocal
set "HERE=%~dp0"
set "DEMO=%HERE%tests\data"

rem ------------------------------- SETTINGS ---------------------------------
set "PYTHON=py -3"
set "TOP=foo"
set NETLISTS="%DEMO%\foo.v" "%DEMO%\sub.v"
set LIBS="%DEMO%\cells.lib"
set BLACKBOX=-blackbox hardip
set MODES=-mode "mode1:SDC_MODE=mode1:%DEMO%\foo.nondft.sdc:%DEMO%\bar.sdc" -mode "mode2:SDC_MODE=mode2:%DEMO%\foo.nondft.sdc" -mode "dft:%DEMO%\foo.dft.sdc"
set "OUT_DIR=sdc_qc_out"
set "JOBS=4"
rem --------------------------------------------------------------------------

set "TOPARG="
if not "%TOP%"=="" set "TOPARG=-top %TOP%"

echo sdc_qc: %PYTHON% "%HERE%sdc_qc.py" %TOPARG% -netlist %NETLISTS% -lib %LIBS% %BLACKBOX% %MODES% -out_dir "%OUT_DIR%" -jobs %JOBS% %*
%PYTHON% "%HERE%sdc_qc.py" %TOPARG% -netlist %NETLISTS% -lib %LIBS% %BLACKBOX% %MODES% -out_dir "%OUT_DIR%" -jobs %JOBS% %*
set RC=%ERRORLEVEL%
echo sdc_qc: exit status %RC% (0 clean, 1 unwaived ERROR findings, 2 tool error); report: %OUT_DIR%\sdc_qc.rpt, dashboard: %OUT_DIR%\sdc_qc.html
endlocal & exit /b %RC%
