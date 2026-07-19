/* oeshared.i  -  shared order-entry context.
   Include with {include/oeshared.i NEW} in the top-level entry program
   (oe/oe-entry.p) and {include/oeshared.i} everywhere downstream.

   HISTORY: 02/11/1997 jmb - created. Everybody in OE order flow leans on
            these instead of parameters, so touch with care.               */

DEFINE {1} SHARED VARIABLE s-ordnum  AS INTEGER NO-UNDO.  /* current order   */
DEFINE {1} SHARED VARIABLE s-custnum AS INTEGER NO-UNDO.  /* current cust    */

/* Shared buffer: positioned by oe-entry.p, read by credit + pricing */
DEFINE {1} SHARED BUFFER sb-cust FOR Customer.
