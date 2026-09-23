# incremental SDC sourced after the main one
set_clock_uncertainty 0.1 [get_clocks CLK]
set_max_transition 0.2 [current_design]
source missing_file.sdc
