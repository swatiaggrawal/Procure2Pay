"""PDF records: one document, or a whole chain from ION to payment."""
import datetime as dt
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SS = getSampleStyleSheet()
BODY = ParagraphStyle("b", parent=SS["BodyText"], fontSize=9.5, leading=12.5)
SMALL = ParagraphStyle("s", parent=BODY, fontSize=8.5, leading=11, textColor=colors.HexColor("#5b6675"))
H1 = ParagraphStyle("h1", parent=SS["Heading1"], fontSize=17, leading=21, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=SS["Heading2"], fontSize=11.5, leading=14, spaceBefore=12, spaceAfter=5, textColor=colors.HexColor("#0f5c6e"))
STATUS = {"DRAFT": "Draft", "PENDING": "In approval", "QUERY": "Query raised", "REJECTED": "Rejected", "APPROVED": "Approved", "PAID": "Paid"}
DECISION = {"pending": "Waiting", "approved": "Approved", "rejected": "Rejected", "query": "Query raised"}
TYPE_NAME = {"ION": "Internal Office Note", "IOM": "Inter-Office Memo", "TRN": "Tender Recommendation Note",
             "PRPO": "SAP PR and PO request", "INVOICE": "Invoice"}


def P(text, style=BODY):
    return Paragraph(escape(str("" if text is None else text)).replace("\n", "<br/>"), style)


def ts(iso):
    return dt.datetime.fromisoformat(iso).strftime("%d %b %Y %H:%M UTC") if iso else ""


def money(n):
    return f"INR {float(n or 0):,.2f}"


def grid(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#d8dde4")),
          ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    if header:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef0f3")))
    t.setStyle(TableStyle(st))
    return t


def section(d):
    f = [P(f"{d['ref']}  {d['title']}", H1),
         P(f"{TYPE_NAME[d['doc_type']]}, {d['company']}. Status: {STATUS[d['status']]}. Created by {d['created_by']} on {ts(d['created_at'])}.", SMALL),
         P("Details", H2)]
    rows = [[P(d["amount_label"], SMALL), P(money(d["amount"]))]]
    if d.get("vendor"):
        vi = d.get("vendor_info") or {}
        rows.append([P("Vendor", SMALL), P(d["vendor"] + (", GSTIN " + vi["gstin"] if vi.get("gstin") else ""))])
    if d.get("tax"):
        t = d["tax"]
        rows.append([P("GST at %g%%" % t["rate"], SMALL), P(money(t["gst"]))])
        rows.append([P("Invoice total incl. GST / payable", SMALL), P("%s / %s (%g%%)" % (money(t["total"]), money(t["payable"]), t["percent_payable"]))])
    if d["data"].get("ses_gr_no"):
        rows.append([P("Service entry / goods receipt", SMALL), P(d["data"]["ses_gr_no"])])
    if d["data"].get("po_number"):
        rows.append([P("PO number", SMALL), P(d["data"]["po_number"])])
    rows += [[P(x["label"], SMALL), P(d["data"].get(x["key"], ""))] for x in d["fields"]]
    ps = d.get("po_summary")
    if ps:
        rows.append([P("PO value / left after this invoice", SMALL), P(f"{money(ps['po_value'])} / {money(ps['remaining'] - d['amount'])} ({ps['after_pct']}% of the PO invoiced)")])
    if d["data"].get("attachments"):
        rows.append([P("Attachments", SMALL), P(", ".join(n.split("_", 2)[-1] for n in d["data"]["attachments"]))])
    f.append(grid(rows, [52 * mm, 118 * mm], header=False))
    f.append(P("Approvals", H2))
    if d["steps"]:
        f.append(grid([[P(h, SMALL) for h in ("Level", "Approver", "Decision", "When", "Comment")]] +
                      [[P(s["level"]), P(s["approver"] + (" (" + s["label"] + ")" if s.get("label") and s["label"] != s["approver"] else "")), P(DECISION[s["decision"]] + (f", by {s['delegate']} as delegate" if s.get("delegate") else "")), P(ts(s["acted_at"])), P(s["comment"])] for s in d["steps"]],
                      [13 * mm, 40 * mm, 27 * mm, 36 * mm, 54 * mm]))
    else:
        f.append(P("Not submitted yet.", SMALL))
    if d["sap"] or d["payment"]:
        f.append(P("SAP and payment", H2))
        for r in d["sap"]:
            if r["kind"] == "PR_PO":
                f.append(P(f"PR {r['pr_number'] or '-'}, PO {r['po_number'] or 'pending approval'}. Raised by {r['created_by_name']}"
                           + (f", approved by {r['approved_by_name']}" if r["approved_by_name"] else "") + f". {ts(r['at'])}"))
            else:
                f.append(P(f"SAP document {r['sap_doc_no']}. Parked by {r['park_user']}, posted by {r['post_user']}. {ts(r['at'])}"))
        if d["payment"]:
            p = d["payment"]
            f.append(P(f"Paid {money(p['amount'])}, reference {p['reference']}, released by {p['paid_by']} on {ts(p['paid_at'])}."))
    if d["comments"]:
        f.append(P("Comments", H2))
        f += [P(f"{c['user']}, {ts(c['at'])}: {c['text']}") for c in d["comments"]]
    return f


def build(docs, generated_by, title=None):
    """docs: list of service.detail() dicts, in chain order."""
    buf = BytesIO()
    def footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#5b6675"))
        canvas.drawString(18 * mm, 10 * mm, f"Procure to Pay portal record. Generated by {generated_by} on {dt.datetime.now(dt.timezone.utc):%d %b %Y %H:%M UTC}")
        canvas.drawRightString(192 * mm, 10 * mm, f"Page {doc.page}")
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title=title or docs[0]["ref"], author="Procure to Pay portal")
    story = []
    if len(docs) > 1:
        story += [P(title or "Purchase record", H1), P(f"{docs[0]['company']}. {len(docs)} documents in this chain.", SMALL), Spacer(1, 8),
                  grid([[P(h, SMALL) for h in ("Document", "Title", "Status", "Amount")]] +
                       [[P(d["ref"]), P(d["title"]), P(STATUS[d["status"]]), P(money(d["amount"]))] for d in docs],
                       [28 * mm, 80 * mm, 30 * mm, 32 * mm]), PageBreak()]
    for i, d in enumerate(docs):
        story += section(d)
        if i < len(docs) - 1:
            story.append(PageBreak())
    pdf.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
