# Clean multi-clock benchmark SDC: expected to produce no ERROR/WARNING
set_units -time ns -capacitance pF
current_design bench
create_clock -name CLKA -period 2.0 [get_ports clk_a]
create_clock -name CLKB -period 3.0 [get_ports clk_b]
create_clock -name CLKC -period 5.0 [get_ports clk_c]
create_generated_clock -name DIVA -source [get_ports clk_a] -divide_by 2 [get_pins div_reg/Q]
create_generated_clock -name MCLKB -source [get_ports clk_b] -combinational [get_pins u_ckmux/Y]
create_generated_clock -name MCLKC -source [get_ports clk_c] -combinational -add [get_pins u_ckmux/Y]
set_clock_groups -asynchronous -group {CLKA DIVA} -group {CLKB MCLKB} -group {CLKC MCLKC}
set_clock_groups -physically_exclusive -group MCLKB -group MCLKC
set_clock_uncertainty 0.1 [all_clocks]
set_input_delay -clock CLKA -max 0.6 [get_ports {din[*] tsel}]
set_input_delay -clock CLKA -min 0.1 [get_ports {din[*] tsel}]
set_output_delay -clock CLKA -max 0.5 [get_ports dout*]
set_output_delay -clock CLKA -min 0.0 [get_ports dout*]
set_driving_cell -lib_cell BUFX2 [all_inputs]
set_load 0.01 [all_outputs]
set_multicycle_path 2 -setup -from [get_clocks CLKA] -to [get_clocks DIVA]
set_multicycle_path 1 -hold  -from [get_clocks CLKA] -to [get_clocks DIVA]
set_false_path -from [get_ports tsel] -to [get_pins -hier u_m/r_reg*/D]
