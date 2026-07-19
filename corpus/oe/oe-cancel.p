/*------------------------------------------------------------------------
    File        : oe/oe-cancel.p
    Purpose     : Cancel an open order. Releases inventory allocation on
                  allocated lines, cancels all lines, cancels the header.
    Author      : dkp
    Created     : 07/02/2003
    Notes       : Only orders still in Ordered status can be cancelled.
                  Shipped or posted orders go through RMA, not here.
                  Backordered lines were never allocated, so they are
                  cancelled without touching inventory.
  ----------------------------------------------------------------------*/

{include/ordstat.i}

DEFINE INPUT PARAMETER ip-ordnum AS INTEGER NO-UNDO.

/* same bootstrap as oe-entry.p - copied 2003, keep in sync by hand */
DEFINE NEW GLOBAL SHARED VARIABLE gh-alloc AS HANDLE NO-UNDO.

FIND Order EXCLUSIVE-LOCK WHERE Order.OrderNum = ip-ordnum NO-ERROR.
IF NOT AVAILABLE Order THEN
    RETURN ERROR "Order " + STRING(ip-ordnum) + " not on file.".

IF Order.OrderStatus <> {&ORD-ORDERED} THEN
    RETURN ERROR "Order " + STRING(ip-ordnum) + " is "
                 + Order.OrderStatus + " - cannot cancel.".

IF NOT VALID-HANDLE(gh-alloc) THEN
    RUN inv/inv-alloc.p PERSISTENT SET gh-alloc.

DO TRANSACTION ON ERROR UNDO, RETURN ERROR:

    FOR EACH OrderLine OF Order EXCLUSIVE-LOCK:

        /* Ordered lines hold allocation; give it back. */
        IF OrderLine.OrderLineStatus = {&LIN-ORDERED} THEN
            RUN deallocate-item IN gh-alloc (INPUT OrderLine.ItemNum,
                                             INPUT OrderLine.Qty).

        ASSIGN OrderLine.OrderLineStatus = {&LIN-CANCELLED}.

    END.

    ASSIGN Order.OrderStatus = {&ORD-CANCELLED}.

END. /* transaction */

RELEASE Order.
RETURN.
