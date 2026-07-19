/*------------------------------------------------------------------------
    File        : oe/oe-ship.p
    Purpose     : Ship confirmation for one order. Relieves inventory for
                  each allocated line, marks lines shipped, and marks the
                  order shipped once every line is done.
    Author      : sw
    Created     : 09/22/2004
    History     : 04/17/2007 dm - inventory relief moved here from
                                  oe-post.p (posting window was too tight).
    Notes       : Backordered lines are NOT shipped and hold the order
                  open; re-run after allocation catches up.
  ----------------------------------------------------------------------*/

{include/ordstat.i}

DEFINE INPUT PARAMETER ip-ordnum AS INTEGER NO-UNDO.

FIND Order EXCLUSIVE-LOCK WHERE Order.OrderNum = ip-ordnum NO-ERROR.
IF NOT AVAILABLE Order THEN
    RETURN ERROR "Order " + STRING(ip-ordnum) + " not on file.".

IF Order.OrderStatus <> {&ORD-ORDERED} THEN
    RETURN ERROR "Order " + STRING(ip-ordnum) + " is "
                 + Order.OrderStatus + " - cannot ship.".

/* Each line ships (or doesn't) in its own transaction so one bad line
   doesn't roll back the whole truck.                                     */
FOR EACH OrderLine OF Order EXCLUSIVE-LOCK
    WHERE OrderLine.OrderLineStatus = {&LIN-ORDERED}
    TRANSACTION:
    RUN ship-line.
END.

/* Header goes to Shipped only when nothing is left open on the order. */
IF NOT CAN-FIND(FIRST OrderLine OF Order
                WHERE OrderLine.OrderLineStatus = {&LIN-ORDERED}
                   OR OrderLine.OrderLineStatus = {&LIN-BACKORD}) THEN
DO TRANSACTION:
    ASSIGN Order.OrderStatus = {&ORD-SHIPPED}
           Order.ShipDate    = TODAY.
END.

RELEASE Order.
RETURN.

/* ---- internal procedures ---- */

PROCEDURE ship-line:
/* Relieves OnHand and Allocated for the current OrderLine and marks it
   shipped. Skips (leaves Ordered) if the Item record is locked.

   NOTE 04/2007 dm: this updates Item DIRECTLY instead of going through
   inv/inv-alloc.p like order entry does. Should be unified some day,
   but the posting window is tight and it works.                          */

    FIND Item WHERE Item.ItemNum = OrderLine.ItemNum
        EXCLUSIVE-LOCK NO-WAIT NO-ERROR.

    IF LOCKED(Item) THEN DO:
        MESSAGE "Item" OrderLine.ItemNum
                "locked - line" OrderLine.LineNum "left open.".
        RETURN.
    END.

    IF NOT AVAILABLE Item THEN DO:
        MESSAGE "Item" OrderLine.ItemNum "missing - line"
                OrderLine.LineNum "left open.".
        RETURN.
    END.

    ASSIGN Item.OnHand              = Item.OnHand    - OrderLine.Qty
           Item.Allocated           = Item.Allocated - OrderLine.Qty
           OrderLine.OrderLineStatus = {&LIN-SHIPPED}.

END PROCEDURE.
