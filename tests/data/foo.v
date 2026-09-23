/////////////////////////////////////////////////////////////
// Created by: Design Compiler (synthetic test netlist)
/////////////////////////////////////////////////////////////
module foo ( clk, clk2, tck, rst_n, scan_en, a, b, sel, y, z, mem_q, test_mode );
  input clk, clk2, tck, rst_n, scan_en, sel, test_mode;
  input [3:0] a;
  input [3:0] b;
  output [3:0] y;
  output z;
  output [7:0] mem_q;
  wire   n1, n2, n3, ckbuf, ckinv, div_q, div_d, mclk, \u_core/n5 , \u_core/q ;
  wire   [3:0] s;
  wire   [3:0] addr;
  assign z = \u_core/q ;
  BUFX2 ckb ( .A(clk), .Y(ckbuf) );
  INVX1 cki ( .A(ckbuf), .Y(ckinv) );
  DFFX1 \u_core/q_reg  ( .CK(ckinv), .D(n1), .Q(\u_core/q ) );
  AND2X1 \u_core/U1  ( .A1(a[0]), .A2(b[0]), .Y(n1) );
  DFFX1 \u_core/div_reg  ( .CK(clk2), .D(div_d), .Q(div_q) );
  INVX1 \u_core/U2  ( .A(div_q), .Y(div_d) );
  DFFX1 \u_core/slow_reg  ( .CK(div_q), .D(a[1]), .Q(n2) );
  MUX2X1 u_ckmux ( .A(clk), .B(tck), .S(test_mode), .Y(mclk) );
  DFFX1 \u_core/mux_reg  ( .CK(mclk), .D(n2), .Q(n3) );
  DFFX1 \u_core/tie_reg  ( .CK(1'b0), .D(n3), .Q(\u_core/n5 ) );
  sub u_sub ( .clk(clk), .en(sel), .din({a[3:1], n3}), .dout(y) );
  SRAM16X8 u_mem ( .CLK(clk), .CEN(rst_n), .A(addr), .D({b, a}), .Q(mem_q) );
  DFFX1 \addr_reg[0]  ( .CK(clk), .D(a[0]), .Q(addr[0]) );
  DFFX1 \addr_reg[1]  ( .CK(clk), .D(a[1]), .Q(addr[1]) );
  DFFX1 \addr_reg[2]  ( .CK(clk), .D(a[2]), .Q(addr[2]) );
  DFFX1 \addr_reg[3]  ( .CK(clk), .D(a[3]), .Q(addr[3]) );
  hardip u_hip ( .CK(clk), .DI(n3), .DO() );
endmodule
