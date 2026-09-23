current_design foo
create_clock -name TCK -period 20 [get_ports tck]
create_clock -name CLK -period 2 [get_ports clk]
set_case_analysis 1 [get_ports test_mode]
set_input_delay -clock TCK 2 [all_inputs]
set_output_delay -clock TCK 2 [all_outputs]
set_multicycle_path 2 -setup -to [get_pins u_core/q_reg/D]
set_multicycle_path 1 -hold -to [get_pins u_core/q_reg/D]
create_clock -name TCK -period 25 [get_ports tck]
get_cells -hier nothing_here*
