/* ordstat.i  -  order / order line status code constants.
   HISTORY: 05/20/1998 jmb - created so we stop typing literals.
            07/02/2003 dkp - added line-level statuses.                    */

/* Order.OrderStatus values */
&GLOBAL-DEFINE ORD-ORDERED   "Ordered"
&GLOBAL-DEFINE ORD-SHIPPED   "Shipped"
&GLOBAL-DEFINE ORD-POSTED    "Posted"
&GLOBAL-DEFINE ORD-CANCELLED "Cancelled"

/* OrderLine.OrderLineStatus values */
&GLOBAL-DEFINE LIN-ORDERED   "Ordered"
&GLOBAL-DEFINE LIN-BACKORD   "Backordered"
&GLOBAL-DEFINE LIN-SHIPPED   "Shipped"
&GLOBAL-DEFINE LIN-CANCELLED "Cancelled"
