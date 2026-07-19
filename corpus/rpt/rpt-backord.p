/* -----------------------------------------------------------------------
   rpt/rpt-backord.p - backorder report. oldest program still running.
   lists every backordered line with customer + item stock position so
   purchasing knows what to expedite.
   author: jmb 05/08/1996
   history: 05/1998 jmb - order statuses moved to include/ordstat.i
            08/2003 dkp - selection now via {&LIN-BACKORD} after the
                          line-level statuses landed in ordstat.i
   ----------------------------------------------------------------------- */

{include/ordstat.i}

def var d-ext as dec no-undo.

output to value("backord.rpt") paged.

put unformatted
    "BACKORDER REPORT  " string(today, "99/99/9999") skip
    "==================================================" skip(1).

for each OrderLine no-lock
        where OrderLine.OrderLineStatus = {&LIN-BACKORD},
    first Order of OrderLine no-lock,
    first Item of OrderLine no-lock,
    first Customer of Order no-lock
    break by OrderLine.ItemNum:

    /* what the line is worth if we could ship it */
    assign d-ext = OrderLine.ExtendedPrice.

    accumulate OrderLine.Qty (total).
    accumulate d-ext (total).

    display Item.ItemNum
            Item.ItemName
            Customer.Name
            Order.OrderNum
            OrderLine.Qty
            Item.OnHand
            Item.OnOrder
        with frame f-det down width 110.

end.

put skip(1)
    "total units backordered: " (accum total OrderLine.Qty) skip
    "total value backordered: " (accum total d-ext) skip.

output close.
