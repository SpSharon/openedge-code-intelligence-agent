/*------------------------------------------------------------------------
    File        : inv/inv-alloc.p
    Purpose     : Inventory allocation manager. Runs PERSISTENT; callers
                  hold the handle in the global shared variable gh-alloc
                  and RUN allocate-item / deallocate-item IN it.
    Author      : sw
    Created     : 11/19/2008
    Notes       : One instance per session. Instantiated lazily by
                  oe/oe-entry.p and oe/oe-cancel.p. Nobody DELETEs the
                  procedure; it lives until the session ends. Shipping
                  (oe/oe-ship.p) still relieves Item directly - see the
                  2007 note over there.
  ----------------------------------------------------------------------*/

/* ---- functions ---- */

FUNCTION check-avail RETURNS LOGICAL
    (INPUT ip-itemnum AS INTEGER, INPUT ip-qty AS INTEGER):
/* Free stock = OnHand - Allocated. No lock; the authoritative check is
   the EXCLUSIVE find in allocate-item.                                   */

    DEFINE BUFFER b-item FOR Item.

    FIND b-item NO-LOCK WHERE b-item.ItemNum = ip-itemnum NO-ERROR.
    IF NOT AVAILABLE b-item THEN RETURN FALSE.

    RETURN b-item.OnHand - b-item.Allocated >= ip-qty.

END FUNCTION.

/* ---- main block: nothing to do at instantiation ---- */

/* ---- internal procedures (the persistent API) ---- */

PROCEDURE allocate-item:
/* Reserve stock for an order line. FALSE = short; caller backorders.     */

    DEFINE INPUT  PARAMETER ip-itemnum AS INTEGER NO-UNDO.
    DEFINE INPUT  PARAMETER ip-qty     AS INTEGER NO-UNDO.
    DEFINE OUTPUT PARAMETER op-ok      AS LOGICAL NO-UNDO.

    IF NOT check-avail(ip-itemnum, ip-qty) THEN DO:
        ASSIGN op-ok = FALSE.
        RETURN.
    END.

    DO TRANSACTION:
        FIND Item WHERE Item.ItemNum = ip-itemnum
            EXCLUSIVE-LOCK NO-WAIT NO-ERROR.

        IF LOCKED(Item) OR NOT AVAILABLE Item THEN DO:
            ASSIGN op-ok = FALSE.
            RETURN.
        END.

        /* re-verify under the exclusive lock; the NO-LOCK read in
           check-avail can go stale between check and lock               */
        IF Item.OnHand - Item.Allocated < ip-qty THEN DO:
            ASSIGN op-ok = FALSE.
            RETURN.
        END.

        ASSIGN Item.Allocated = Item.Allocated + ip-qty
               op-ok          = TRUE.
    END.

END PROCEDURE.

PROCEDURE deallocate-item:
/* Give allocation back (order cancel). Floors at zero because 20 years
   of history mean the numbers do not always add up.                      */

    DEFINE INPUT PARAMETER ip-itemnum AS INTEGER NO-UNDO.
    DEFINE INPUT PARAMETER ip-qty     AS INTEGER NO-UNDO.

    DO TRANSACTION:
        FIND Item WHERE Item.ItemNum = ip-itemnum
            EXCLUSIVE-LOCK NO-ERROR.

        IF AVAILABLE Item THEN
            ASSIGN Item.Allocated = MAXIMUM(Item.Allocated - ip-qty, 0).
    END.

END PROCEDURE.
