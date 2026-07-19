/*------------------------------------------------------------------------
    File        : oe/oe-entry.p
    Purpose     : Order entry - creates Order header + OrderLines for a
                  customer, prices each line, allocates inventory.
    Author      : jmb
    Created     : 03/14/1997
    History     : 04/02/2001 dkp - credit check now runs BEFORE lines are
                                   added (was after; see incident 2001-03).
                  11/19/2008 sw  - line allocation goes through persistent
                                   inv/inv-alloc.p instead of direct writes.
                  06/25/2012 tmg - character UI stripped out; caller passes
                                   a temp-table of requested lines.
    Notes       : Order number comes off sequence next-ord-num inside the
                  transaction; a declined credit check UNDOes the header but
                  the sequence value stays burned. AR knows and doesn't care.
  ----------------------------------------------------------------------*/

{include/oeshared.i NEW}
{include/ordstat.i}

DEFINE TEMP-TABLE tt-line NO-UNDO
    FIELD ItemNum AS INTEGER
    FIELD Qty     AS INTEGER.

DEFINE INPUT  PARAMETER ip-custnum AS INTEGER NO-UNDO.
DEFINE INPUT  PARAMETER TABLE FOR tt-line.
DEFINE OUTPUT PARAMETER op-ordnum  AS INTEGER NO-UNDO.

DEFINE NEW GLOBAL SHARED VARIABLE gh-alloc AS HANDLE NO-UNDO.

DEFINE VARIABLE l-approved AS LOGICAL NO-UNDO.
DEFINE VARIABLE i-line     AS INTEGER NO-UNDO.

/* ---- main block ---- */

FIND sb-cust WHERE sb-cust.CustNum = ip-custnum NO-LOCK NO-ERROR.
IF NOT AVAILABLE sb-cust THEN
    RETURN ERROR "Unknown customer " + STRING(ip-custnum) + ".".

ASSIGN s-custnum = ip-custnum.

DO TRANSACTION ON ERROR UNDO, RETURN ERROR:

    RUN create-order-header.

    RUN oe/oe-credit.p (OUTPUT l-approved).
    IF NOT l-approved THEN
        UNDO, RETURN ERROR "Credit declined for customer "
                           + STRING(sb-cust.CustNum) + ".".

    FOR EACH tt-line:
        ASSIGN i-line = i-line + 1.
        RUN add-order-line (INPUT tt-line.ItemNum,
                            INPUT tt-line.Qty,
                            INPUT i-line).
    END.

END. /* transaction */

ASSIGN op-ordnum = s-ordnum.
RELEASE Order.

RETURN.

/* ---- internal procedures ---- */

PROCEDURE create-order-header:
/* Creates the Order header and positions the shared order number.        */

    ASSIGN s-ordnum = NEXT-VALUE(next-ord-num).

    CREATE Order.
    ASSIGN Order.OrderNum    = s-ordnum
           Order.CustNum     = sb-cust.CustNum
           Order.OrderDate   = TODAY
           Order.PromiseDate = TODAY + 7
           Order.Terms       = sb-cust.Terms
           Order.SalesRep    = sb-cust.SalesRep
           Order.OrderStatus = {&ORD-ORDERED}.

END PROCEDURE.

PROCEDURE add-order-line:
/* Prices one requested line, allocates stock, creates the OrderLine.
   Short allocation does NOT fail the order - line goes on backorder.     */

    DEFINE INPUT PARAMETER ip-itemnum AS INTEGER NO-UNDO.
    DEFINE INPUT PARAMETER ip-qty     AS INTEGER NO-UNDO.
    DEFINE INPUT PARAMETER ip-linenum AS INTEGER NO-UNDO.

    DEFINE VARIABLE d-price AS DECIMAL NO-UNDO.
    DEFINE VARIABLE i-disc  AS INTEGER NO-UNDO.
    DEFINE VARIABLE l-alloc AS LOGICAL NO-UNDO.

    FIND Item WHERE Item.ItemNum = ip-itemnum NO-LOCK NO-ERROR.
    IF NOT AVAILABLE Item THEN DO:
        MESSAGE "Item" ip-itemnum "not on file - line" ip-linenum "skipped.".
        RETURN.
    END.

    RUN oe/oe-price.p (INPUT  ip-itemnum,
                       INPUT  ip-qty,
                       OUTPUT d-price,
                       OUTPUT i-disc).
    IF d-price = ? THEN DO:
        MESSAGE "No price for item" ip-itemnum "- line" ip-linenum "skipped.".
        RETURN.
    END.

    /* 11/2008 sw: all allocation goes through the allocation manager now */
    IF NOT VALID-HANDLE(gh-alloc) THEN
        RUN inv/inv-alloc.p PERSISTENT SET gh-alloc.

    RUN allocate-item IN gh-alloc (INPUT  ip-itemnum,
                                   INPUT  ip-qty,
                                   OUTPUT l-alloc).

    CREATE OrderLine.
    ASSIGN OrderLine.OrderNum        = s-ordnum
           OrderLine.LineNum         = ip-linenum
           OrderLine.ItemNum         = ip-itemnum
           OrderLine.Qty             = ip-qty
           OrderLine.Price           = d-price
           OrderLine.Discount        = i-disc
           OrderLine.ExtendedPrice   = ROUND(ip-qty * d-price
                                             * (1 - i-disc / 100), 2)
           OrderLine.OrderLineStatus = IF l-alloc THEN {&LIN-ORDERED}
                                       ELSE {&LIN-BACKORD}.

END PROCEDURE.
