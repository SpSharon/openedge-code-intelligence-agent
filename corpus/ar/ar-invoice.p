/*------------------------------------------------------------------------
    File        : ar/ar-invoice.p
    Purpose     : Create the AR invoice for one shipped order: writes the
                  ArHist record and rolls the amount into the customer
                  balance.
    Author      : jmb
    Created     : 11/03/1998
    History     : 03/12/2009 sw - callers now retry on lock failure, so we
                                  just return 0 instead of messaging.
    Notes       : Runs INSIDE the caller's transaction (oe/oe-post.p).
                  No commit happens here; if the caller UNDOes, the
                  ArHist record and the balance update roll back too.
  ----------------------------------------------------------------------*/

DEFINE INPUT  PARAMETER ip-ordnum AS INTEGER NO-UNDO.
DEFINE OUTPUT PARAMETER op-invnum AS INTEGER NO-UNDO.  /* 0 = failed */

/* ---- functions ---- */

FUNCTION calc-order-total RETURNS DECIMAL (INPUT ip-ord AS INTEGER):
/* Sum of extended prices on the order. NOTE: service/OrderService.cls
   carries its own copy of this math - keep in sync by hand.              */

    DEFINE VARIABLE d-total AS DECIMAL NO-UNDO.
    DEFINE BUFFER b-line FOR OrderLine.

    FOR EACH b-line NO-LOCK WHERE b-line.OrderNum = ip-ord:
        ASSIGN d-total = d-total + b-line.ExtendedPrice.
    END.

    RETURN d-total.

END FUNCTION.

/* ---- main block ---- */

FIND Order NO-LOCK WHERE Order.OrderNum = ip-ordnum NO-ERROR.
IF NOT AVAILABLE Order THEN DO:
    ASSIGN op-invnum = 0.
    RETURN.
END.

FIND Customer EXCLUSIVE-LOCK
    WHERE Customer.CustNum = Order.CustNum NO-WAIT NO-ERROR.
IF LOCKED(Customer) OR NOT AVAILABLE Customer THEN DO:
    ASSIGN op-invnum = 0.
    RETURN.
END.

CREATE ArHist.
ASSIGN ArHist.InvoiceNum  = NEXT-VALUE(next-inv-num)
       ArHist.CustNum     = Order.CustNum
       ArHist.OrderNum    = Order.OrderNum
       ArHist.InvoiceDate = TODAY
       ArHist.Amount      = calc-order-total(ip-ordnum)
       ArHist.SalesRep    = Order.SalesRep.

/* the one place Customer.Balance goes UP; cash receipts bring it down
   (that system lives in the GL package, not here)                        */
ASSIGN Customer.Balance = Customer.Balance + ArHist.Amount.

ASSIGN op-invnum = ArHist.InvoiceNum.

RETURN.
