/*------------------------------------------------------------------------
    File        : oe/oe-credit.p
    Purpose     : Credit check for order entry. Approves or declines based
                  on AR balance plus open-order exposure vs credit limit.
    Author      : jmb
    Created     : 08/06/1998
    History     : 04/02/2001 dkp - now called before lines exist; exposure
                                   covers PRIOR open orders only, the order
                                   being entered has no priced lines yet.
                  10/14/2009 sw  - added d-tolerance after the Meyer's
                                   Hardware escalation. Do not remove.
    Notes       : Relies on the shared buffer sb-cust and shared s-ordnum
                  positioned by oe/oe-entry.p ({include/oeshared.i}).
  ----------------------------------------------------------------------*/

{include/oeshared.i}
{include/ordstat.i}

DEFINE OUTPUT PARAMETER op-approved AS LOGICAL NO-UNDO.

DEFINE VARIABLE d-tolerance AS DECIMAL NO-UNDO INITIAL 100.

/* ---- functions ---- */

FUNCTION get-open-balance RETURNS DECIMAL ():
/* Total extended price on this customer's open (not posted, not
   cancelled) orders. Uses its own buffers so we don't move anybody's
   Order position under them.                                             */

    DEFINE VARIABLE d-open AS DECIMAL NO-UNDO.
    DEFINE BUFFER b-ord  FOR Order.
    DEFINE BUFFER b-line FOR OrderLine.

    FOR EACH b-ord NO-LOCK
        WHERE b-ord.CustNum = sb-cust.CustNum
          AND LOOKUP(b-ord.OrderStatus,
                     {&ORD-POSTED} + "," + {&ORD-CANCELLED}) = 0,
        EACH b-line OF b-ord NO-LOCK:
        ASSIGN d-open = d-open + b-line.ExtendedPrice.
    END.

    RETURN d-open.

END FUNCTION.

/* ---- main block ---- */

IF NOT AVAILABLE sb-cust THEN
    RETURN ERROR "oe-credit.p called with no customer positioned.".

RUN check-credit.

RETURN.

/* ---- internal procedures ---- */

PROCEDURE check-credit:
/* The actual credit decision. Balance + open exposure must stay inside
   CreditLimit plus tolerance.                                            */

    IF sb-cust.Balance + get-open-balance()
       > sb-cust.CreditLimit + d-tolerance THEN DO:
        ASSIGN op-approved = FALSE.
        MESSAGE "Credit declined: order" s-ordnum
                "customer" sb-cust.CustNum
                "balance" sb-cust.Balance
                "limit" sb-cust.CreditLimit.
    END.
    ELSE
        ASSIGN op-approved = TRUE.

END PROCEDURE.
