/* OrderService.cls - OO facade over the legacy order-entry programs.
   Added 2019 for the order-portal integration pilot so the web tier
   never RUNs a bare .p. Wraps, does not replace: all business logic
   stays in the .p files.  mv 06/2019                                     */

block-level on error undo, throw.

class service.OrderService:

    define temp-table ttOrderLine no-undo
        field ItemNum as integer
        field Qty     as integer.

    /*----------------------------------------------------------------
      Creates an order for a customer from the requested lines.
      Delegates to oe/oe-entry.p; a RETURN ERROR there (unknown
      customer, credit declined) surfaces here as a thrown AppError.
    ----------------------------------------------------------------*/
    method public integer CreateOrder (input ipCustNum as integer,
                                       input table ttOrderLine):

        define variable iOrdNum as integer no-undo.

        run oe/oe-entry.p (input ipCustNum,
                           input table ttOrderLine,
                           output iOrdNum).

        return iOrdNum.

    end method.

    /*----------------------------------------------------------------
      Cancels an open order. Delegates to oe/oe-cancel.p.
    ----------------------------------------------------------------*/
    method public void CancelOrder (input ipOrdNum as integer):

        run oe/oe-cancel.p (input ipOrdNum).

    end method.

    /*----------------------------------------------------------------
      Order total for the portal's cart display.
      NOTE: same math as calc-order-total in ar/ar-invoice.p, minus
      cancelled lines - kept in sync by hand since 2019.
    ----------------------------------------------------------------*/
    method public decimal GetOrderTotal (input ipOrdNum as integer):

        define variable dTotal as decimal no-undo.
        define buffer bLine for OrderLine.

        for each bLine no-lock
            where bLine.OrderNum = ipOrdNum
              and bLine.OrderLineStatus <> "Cancelled":
            assign dTotal = dTotal + bLine.ExtendedPrice.
        end.

        return dTotal.

    end method.

end class.
