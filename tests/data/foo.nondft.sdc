# Functional SDC with SDC_MODE selection (mode1 / mode2)
set_units -time ns -capacitance pF
current_design foo

if {$SDC_MODE eq "mode1"} {
    set PERIOD 2.0
} elseif {$SDC_MODE eq "mode2"} {
    set PERIOD 4.0
} else {
    error "unknown SDC_MODE $SDC_MODE"
}

create_clock -name CLK -period $PERIOD -waveform [list 0 [expr {$PERIOD/2}]] [get_ports clk]
create_clock -name CLK2 -period 10 [get_ports clk2]
create_generated_clock -name DIV2 -source [get_pins u_core/div_reg/CK] -divide_by 2 [get_pins u_core/div_reg/Q]
create_clock -name VCLK -period 5
create_clock -name UNUSED_V -period 5

proc io_delays {clk pct} {
    set p [get_attribute [get_clocks $clk] period]
    set_input_delay -clock $clk -max [expr {$p * $pct}] [remove_from_collection [all_inputs -no_clocks] [get_ports {rst_n scan_en test_mode}]]
    set_input_delay -clock $clk -min 0.1 [get_ports a*]
}
io_delays CLK 0.3
set_output_delay -clock VCLK 4.5 [get_ports y*]
set_output_delay -clock CLK 0.2 [get_ports z]

# deliberate problems below ------------------------------------------------
set_input_delay -clock CLK 0.5 [get_ports y[0]]
set_false_path -from [get_ports rst_nn]
set_false_path -from [get_pins u_core/U1/Y] -to [get_pins u_core/q_reg/D]
set_multicycle_path 2 -setup -from [get_clocks CLK] -to [get_clocks CLK2]
set_clock_groups -asynchronous -group [get_clocks CLK] -group [get_clocks {CLK2 DIV2}]
set_false_path -from [get_clocks CLK] -to [get_clocks CLK2]
set_case_analysis 0 [get_ports test_mode]
set_case_analysis high [get_ports scan_en]
set_disable_timing -from A -to Z [get_cells u_ckmux]
foreach_in_collection r [get_cells -hier *_reg* -filter "is_sequential==true && ref_name=~DFF*"] {
    set_max_delay 1.0 -to [get_pins [get_object_name $r]/D]
}
set_load 0.01 [all_outputs]
set_driving_cell -lib_cell BUFX2 [all_inputs]
report_timing
set_propagated_clock [all_clocks]
