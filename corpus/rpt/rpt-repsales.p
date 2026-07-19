/*------------------------------------------------------------------------
    File        : rpt/rpt-repsales.p
    Purpose     : Sales by rep for one month, against quota. Reads posted
                  invoice history (ArHist), not orders.
    Author      : sw
    Created     : 03/11/2005
    Notes       : TODO: the MONTH()/YEAR() functions in the WHERE keep the
                  InvoiceDate index from bracketing, so this walks every
                  ArHist row per rep. Fine at our volumes. sw 2005
  ----------------------------------------------------------------------*/

DEFINE INPUT PARAMETER ip-month AS INTEGER NO-UNDO.
DEFINE INPUT PARAMETER ip-year  AS INTEGER NO-UNDO.

DEFINE VARIABLE d-sales AS DECIMAL NO-UNDO.
DEFINE VARIABLE d-quota AS DECIMAL NO-UNDO.
DEFINE VARIABLE d-pct   AS DECIMAL NO-UNDO.

IF ip-month < 1 OR ip-month > 12 THEN
    RETURN ERROR "Month must be 1-12.".

OUTPUT TO VALUE("repsales.rpt") PAGED.

PUT UNFORMATTED
    "SALES BY REP  " STRING(ip-month) "/" STRING(ip-year) SKIP(1).

FOR EACH SalesRep NO-LOCK:

    ASSIGN d-sales = 0.

    FOR EACH ArHist NO-LOCK
        WHERE ArHist.SalesRep = SalesRep.SalesRep
          AND MONTH(ArHist.InvoiceDate) = ip-month
          AND YEAR(ArHist.InvoiceDate)  = ip-year:
        ASSIGN d-sales = d-sales + ArHist.Amount.
    END.

    ASSIGN d-quota = SalesRep.MonthQuota[ip-month]
           d-pct   = IF d-quota > 0
                     THEN ROUND(d-sales / d-quota * 100, 1)
                     ELSE 0.

    DISPLAY SalesRep.SalesRep
            SalesRep.RepName
            SalesRep.Region
            d-sales LABEL "MTD Sales"
            d-quota LABEL "Quota"
            d-pct   LABEL "Pct" FORMAT "->>9.9"
        WITH FRAME f-rep DOWN WIDTH 100.

END.

OUTPUT CLOSE.
