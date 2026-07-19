/*------------------------------------------------------------------------
    File        : oe/oe-post.p
    Purpose     : Order posting batch. Invoices every shipped order and
                  marks it posted. Run nightly from the batch menu.
    Author      : jmb
    Created     : 02/09/1999
    History     : 06/30/2003 dkp - defensive line-status check after the
                                   half-shipped double-post incident.
                  04/17/2007 dm  - inventory relief moved OUT to oe-ship.p
                                   (old code left below for reference).
                  03/12/2009 sw  - retry loop around invoicing; UNDO the
                                   whole order on third failure.
    Notes       : The FOR EACH block is the transaction. Everything for one
                  order - invoice (via RUN ar/ar-invoice.p), status flip,
                  exit hook - commits or rolls back TOGETHER. Yes, the
                  transaction spans the external RUN. That is on purpose;
                  see 1999 design note in the ops binder.
  ----------------------------------------------------------------------*/

{include/ordstat.i}

DEFINE VARIABLE c-exit-proc AS CHARACTER NO-UNDO INITIAL "oe/oe-exit.p".
DEFINE VARIABLE i-invnum    AS INTEGER   NO-UNDO.
DEFINE VARIABLE i-try       AS INTEGER   NO-UNDO.
DEFINE VARIABLE i-posted    AS INTEGER   NO-UNDO.
DEFINE VARIABLE i-skipped   AS INTEGER   NO-UNDO.

DEFINE STREAM s-log.

OUTPUT STREAM s-log TO VALUE("oe-post.log") APPEND.

PUT STREAM s-log UNFORMATTED
    "===== oe-post run " STRING(TODAY, "99/99/9999") " " STRING(TIME, "HH:MM:SS")
    " =====" SKIP.

post-blk:
FOR EACH Order EXCLUSIVE-LOCK
    WHERE Order.OrderStatus = {&ORD-SHIPPED}
    TRANSACTION
    ON ERROR UNDO post-blk, NEXT post-blk:

    /* 06/2003 dkp: belt and braces - a "shipped" order with an open or
       backordered line means somebody shipped around the system. Skip it,
       do not invoice it.                                                 */
    line-check:
    DO ON ERROR UNDO post-blk, NEXT post-blk:
        FOR EACH OrderLine OF Order NO-LOCK:
            IF OrderLine.OrderLineStatus = {&LIN-ORDERED}
            OR OrderLine.OrderLineStatus = {&LIN-BACKORD} THEN DO:
                ASSIGN i-skipped = i-skipped + 1.
                PUT STREAM s-log UNFORMATTED
                    "order " Order.OrderNum " line " OrderLine.LineNum
                    " still open - order skipped" SKIP.
                UNDO post-blk, NEXT post-blk.
            END.
        END.
    END. /* line-check */

    /* 03/2009 sw: ar-invoice hits lock timeouts under month-end load.
       Three tries, then give up on this order and move on.               */
    ASSIGN i-try = 0.
    inv-blk:
    DO ON ERROR UNDO inv-blk, RETRY inv-blk:

        IF RETRY THEN DO:
            ASSIGN i-try = i-try + 1.
            IF i-try >= 3 THEN DO:
                PUT STREAM s-log UNFORMATTED
                    "order " Order.OrderNum
                    " failed invoicing 3x - rolled back" SKIP.
                UNDO post-blk, NEXT post-blk.
            END.
        END.

        RUN ar/ar-invoice.p (INPUT Order.OrderNum, OUTPUT i-invnum).

        /* 0 = ar-invoice could not lock the customer. Retry THIS block
           (up to the i-try cap above), not the whole order scan.         */
        IF i-invnum = 0 THEN
            UNDO inv-blk, RETRY inv-blk.

    END. /* inv-blk */

    /* pre-04/2007 inventory relief - kept for reference, dm:
    FOR EACH OrderLine OF Order NO-LOCK:
        FIND Item WHERE Item.ItemNum = OrderLine.ItemNum EXCLUSIVE-LOCK.
        ASSIGN Item.OnHand    = Item.OnHand    - OrderLine.Qty
               Item.Allocated = Item.Allocated - OrderLine.Qty.
    END.
    */

    ASSIGN Order.OrderStatus = {&ORD-POSTED}
           i-posted          = i-posted + 1.

    /* Site-specific posting exit. Deliberately NOT in source control -
       each site drops its own oe/oe-exit.p on the PROPATH.               */
    IF SEARCH(c-exit-proc) <> ? THEN
        RUN VALUE(c-exit-proc) (INPUT Order.OrderNum).

    PUT STREAM s-log UNFORMATTED
        "order " Order.OrderNum " posted, invoice " i-invnum SKIP.

END. /* post-blk */

PUT STREAM s-log UNFORMATTED
    "posted " i-posted " orders, skipped " i-skipped SKIP.

OUTPUT STREAM s-log CLOSE.

RETURN.
