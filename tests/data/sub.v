// sub-block netlist (hierarchical instance u_sub in foo)
module sub ( clk, en, din, dout );
  input clk, en;
  input [3:0] din;
  output [3:0] dout;
  wire gclk;
  ICGX1 u_icg ( .CK(clk), .E(en), .GCLK(gclk) );
  DFFX1 \r_reg[0]  ( .CK(gclk), .D(din[0]), .Q(dout[0]) );
  DFFX1 \r_reg[1]  ( .CK(gclk), .D(din[1]), .Q(dout[1]) );
  DFFX1 \r_reg[2]  ( .CK(gclk), .D(din[2]), .Q(dout[2]) );
  DFFX1 \r_reg[3]  ( .CK(gclk), .D(din[3]), .Q(dout[3]) );
endmodule
