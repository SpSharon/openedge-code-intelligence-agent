/* -----------------------------------------------------------------------
   oe/oe-price.p - pricing engine, broken out of oe-entry.p 06/2001 dkp.
   Returns unit list price and total discount pct for one item/qty.
   Discount = customer standard discount + volume break, capped at 25
   per credit memo 2001-14.
   Uses shared sb-cust ({include/oeshared.i}) for the customer discount -
   caller must have the customer positioned.
   history: 03/11/2005 sw - volume breaks moved to 50/100 units.
   ----------------------------------------------------------------------- */

{include/oeshared.i}

def input  param ip-itemnum  as int no-undo.
def input  param ip-qty      as int no-undo.
def output param op-price    as dec no-undo.
def output param op-discount as int no-undo.

/* ---- functions ---- */

function get-list-price returns decimal (input ip-item as int):
    /* unknown value means item missing - caller decides what to do */
    def buffer b-item for Item.

    find b-item no-lock where b-item.ItemNum = ip-item no-error.
    if not avail b-item then return ?.
    return b-item.Price.
end function.

/* ---- main block ---- */

assign op-price = get-list-price(ip-itemnum).
if op-price = ? then return.

run apply-discount.

return.

/* ---- internal procedures ---- */

procedure apply-discount:
/* customer standard discount plus volume break on qty */

    assign op-discount = sb-cust.Discount.

    if ip-qty >= 100 then
        assign op-discount = op-discount + 10.
    else if ip-qty >= 50 then
        assign op-discount = op-discount + 5.

    /* cap - credit memo 2001-14 */
    assign op-discount = minimum(op-discount, 25).

end procedure.
