"""Business rules. Routes in app.py stay thin; seed.py reuses these too."""
import datetime as dt
import os
import re

from fastapi import HTTPException
from sqlalchemy import func, select

import security
from models import *

# Core fields shared by every company. Companies add their own on top (FieldConfig).
TEMPLATES = {
    "ION": [
        {"key": "department", "label": "Department", "type": "text"},
        {"key": "justification", "label": "Purpose and justification", "type": "textarea"},
        {"key": "vendors", "label": "Vendors to approach", "type": "textarea"},
    ],
    "IOM": [
        {"key": "quotes", "label": "Vendor quotes (one per line: vendor, price)", "type": "textarea"},
        {"key": "l1_vendor", "label": "Selected vendor (L1)", "type": "text"},
        {"key": "justification", "label": "Why this vendor", "type": "textarea"},
    ],
    "TRN": [
        {"key": "tender_ref", "label": "Tender reference", "type": "text"},
        {"key": "recommended_vendor", "label": "Recommended vendor", "type": "text"},
        {"key": "recommendation", "label": "Recommendation", "type": "textarea"},
    ],
    "PRPO": [
        {"key": "items", "label": "Items or services to order", "type": "textarea"},
        {"key": "delivery", "label": "Delivery location and timeline", "type": "text"},
    ],
    "INVOICE": [
        {"key": "invoice_no", "label": "Invoice number", "type": "text"},
        {"key": "invoice_date", "label": "Invoice date", "type": "date"},
        {"key": "hsn_sac", "label": "HSN or SAC code", "type": "text"},
        {"key": "gst_rate", "label": "GST rate (%)", "type": "select", "options": [0, 5, 12, 18, 28]},
        {"key": "irn", "label": "IRN (e-invoice reference, 64 characters)", "type": "text", "optional": True},
        {"key": "payment_terms", "label": "Payment terms", "type": "text"},
        {"key": "percent_payable", "label": "Percent payable against this invoice", "type": "number"},
        {"key": "scope_of_work", "label": "Scope of work done", "type": "textarea"},
        {"key": "work_completed", "label": "Work completion status", "type": "text"},
    ],
}
AMOUNT_LABEL = {"ION": "Budget (INR)", "IOM": "L1 amount (INR)", "PRPO": "PO value before GST (INR)", "TRN": "Amount (INR)",
                "INVOICE": "Taxable value before GST (INR)"}
EDITABLE = ("DRAFT", "QUERY", "REJECTED")
ACTIVE = ("PENDING", "QUERY", "APPROVED", "PAID")
STUCK_DAYS = float(os.getenv("STUCK_DAYS", "2"))  # pending longer than this is flagged as stuck
MATCH_TOLERANCE_PCT = float(os.getenv("MATCH_TOLERANCE_PCT", "2"))  # three-way match tolerance
GST_RATES = (0, 5, 12, 18, 28)
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")  # format only, the checksum digit is not verified
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
IRN_RE = re.compile(r"^[0-9a-fA-F]{64}$")
CREATOR_TYPES = ("ION", "IOM", "TRN", "PRPO")  # raised by the company's requesters; invoices come from vendors
GLOBAL_ROLES = ("admin", "auditor")


def err(code, msg):
    raise HTTPException(code, msg)


def log(db, user, action, detail="", doc=None):
    db.add(AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else "system",
        company_id=doc.company_id if doc else (user.company_id if user else None),
        document_id=doc.id if doc else None,
        action=action, detail=detail, at=now(),
    ))


def notify(db, user_id, subject, body, doc=None, to_address=None):
    if to_address is None:
        to_address = db.get(AppUser, user_id).email
    db.add(Notification(user_id=user_id, to_address=to_address, subject=subject, body=body,
                        document_id=doc.id if doc else None, created_at=now()))


def name(db, uid):
    u = db.get(AppUser, uid) if uid else None
    return u.name if u else "-"


def ref(d):
    return f"{d.doc_type}-{d.id}"


def delegate_of(db, approver_id):
    r = db.scalar(select(Delegation).where(Delegation.approver_id == approver_id))
    return r.delegate_id if r else None


# ---------- fields, tax ----------

def slug(label):
    return "x_" + (re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "field")[:40]


def extras_for(db, company_id, doc_type):
    rows = db.scalars(select(FieldConfig).where(FieldConfig.company_id == company_id, FieldConfig.doc_type == doc_type)
                      .order_by(FieldConfig.position, FieldConfig.id)).all()
    return [{"key": r.key, "label": r.label, "type": r.ftype, "optional": not r.required, "extra": True} for r in rows]


def fields_for(db, company_id, doc_type):
    return TEMPLATES[doc_type] + extras_for(db, company_id, doc_type)


def tax(d):
    """Amounts are before GST. Payment is on the invoice total, so GST is added here."""
    rate = 0.0
    try:
        rate = float(d.data.get("gst_rate") or 0)
    except (TypeError, ValueError):
        pass
    gst = round(d.amount * rate / 100, 2)
    total = round(d.amount + gst, 2)
    try:
        pct = float(d.data.get("percent_payable") or 100)
    except (TypeError, ValueError):
        pct = 100.0
    return {"rate": rate, "gst": gst, "total": total, "percent_payable": pct, "payable": round(total * pct / 100, 2)}


def norm_inv(s):
    return re.sub(r"\s+", "", str(s or "")).upper()


# ---------- visibility ----------

def can_see(user, d):
    if user.role in GLOBAL_ROLES:
        return True
    if user.role == "vendor":
        return d.vendor_id == user.vendor_id and (d.doc_type == "INVOICE" or (d.doc_type == "PRPO" and d.status in ("APPROVED", "PAID")))
    return user.company_id == d.company_id


def visible(db, user):
    q = select(Document).order_by(Document.updated_at.desc())
    if user.role in GLOBAL_ROLES:
        return q
    if user.role == "vendor":
        return q.where(Document.vendor_id == user.vendor_id,
                       (Document.doc_type == "INVOICE") | ((Document.doc_type == "PRPO") & (Document.status == "APPROVED")))
    return q.where(Document.company_id == user.company_id)


# ---------- approval chain ----------

def cfg_for(db, d):
    """Approval levels that apply at this amount, and the ones skipped because the amount is below their limit."""
    allc = db.scalars(select(ApprovalConfig).where(ApprovalConfig.company_id == d.company_id,
                      ApprovalConfig.doc_type == d.doc_type).order_by(ApprovalConfig.level)).all()
    return [c for c in allc if (c.min_amount or 0) <= d.amount], [c for c in allc if (c.min_amount or 0) > d.amount]


def resolve_approver(db, c, d):
    if c.kind == "requester":
        po = db.get(Document, d.parent_id)
        return po.created_by
    return c.approver_id


def steps_of(db, d):
    return db.scalars(select(ApprovalStep).where(ApprovalStep.document_id == d.id).order_by(ApprovalStep.level)).all()


def current_step(db, d):
    return next((s for s in steps_of(db, d) if s.level == d.current_level), None)


def sod_reason(db, user, d, step):
    """Segregation of duties: why this user may not decide on this step, or None."""
    if user.id == d.created_by:
        return "You raised this document, so you cannot approve it"
    for s in steps_of(db, d):
        if s.id != step.id and s.decision == "approved" and user.id in (s.approver_id, s.acted_by):
            return "You already approved an earlier step of this document, a second person must approve this one"
    return None


def can_act(db, user, d, step):
    if not step or not (step.approver_id == user.id or delegate_of(db, step.approver_id) == user.id):
        return False
    return sod_reason(db, user, d, step) is None


def notify_approver(db, approver_id, subject, body, doc):
    """Mail the approver, and their delegate too when one is set."""
    notify(db, approver_id, subject, body, doc)
    dg = delegate_of(db, approver_id)
    if dg:
        notify(db, dg, subject, f"{body}\n(You are covering for {name(db, approver_id)}.)", doc)


def sap_rec(db, d, kind):
    return db.scalar(select(SapRecord).where(SapRecord.document_id == d.id, SapRecord.kind == kind))


# ---------- vendors ----------

def vendor_out(v, full=False):
    o = {"id": v.id, "name": v.name, "gstin": v.gstin, "email": v.email, "einvoice_required": v.einvoice_required,
         "bank_verified": v.bank_verified, "bank_pending": bool(v.pending_bank_account)}
    if full:
        mask = lambda a: ("x" * max(len(a) - 4, 0) + a[-4:]) if a else ""
        o.update({"bank_account": mask(v.bank_account), "ifsc": v.ifsc or "",
                  "pending_account": mask(v.pending_bank_account), "pending_ifsc": v.pending_ifsc or ""})
    return o


def check_gstin(g):
    g = (g or "").strip().upper()
    if not GSTIN_RE.match(g):
        err(400, "GSTIN must be 15 characters in the standard format, for example 29ABCDE1234F1Z5")
    return g


def create_vendor(db, user, name_, gstin, email, einvoice_required, username, password):
    if user.role != "admin":
        err(403, "Only the admin can add vendors")
    username = (username or "").strip().lower()
    if not name_.strip() or not email.strip() or not username:
        err(400, "Vendor name, email and login name are required")
    if len(password or "") < security.MIN_PASSWORD:
        err(400, f"Password must be at least {security.MIN_PASSWORD} characters")
    if db.scalar(select(AppUser.id).where(AppUser.username == username)):
        err(400, "That login name is taken")
    v = Vendor(name=name_.strip(), gstin=check_gstin(gstin), email=email.strip(), einvoice_required=bool(einvoice_required))
    db.add(v)
    db.flush()
    db.add(AppUser(vendor_id=v.id, company_id=None, username=username, name=v.name, email=v.email, role="vendor",
                   password_hash=security.hash_pw(password)))
    log(db, user, "VENDOR_ADD", f"Vendor {v.name} added with login {username}")
    db.commit()
    return v


def set_bank(db, user, account, ifsc):
    """The vendor proposes bank details. Nothing can be paid to them until Accounts verifies the change."""
    if user.role != "vendor":
        err(403, "Only a vendor can change its own bank details")
    account, ifsc = (account or "").strip(), (ifsc or "").strip().upper()
    if not re.fullmatch(r"[0-9]{9,18}", account):
        err(400, "Account number must be 9 to 18 digits")
    if not IFSC_RE.match(ifsc):
        err(400, "IFSC must look like HDFC0001234")
    v = db.get(Vendor, user.vendor_id)
    v.pending_bank_account, v.pending_ifsc, v.pending_at = account, ifsc, now()
    log(db, user, "BANK_CHANGE", f"{v.name} proposed new bank details (ending {account[-4:]}), awaiting verification")
    for a in db.scalars(select(AppUser).where(AppUser.role == "accounts")):
        notify(db, a.id, f"Verify bank details: {v.name}", f"{v.name} submitted new bank details. Please verify them before any payment is released.")
    db.commit()
    return v


def verify_bank(db, user, vendor_id, approve):
    if user.role != "accounts":
        err(403, "Only Accounts can verify bank details")
    v = db.get(Vendor, vendor_id)
    if not v or not v.pending_bank_account:
        err(400, "Nothing to verify")
    if approve:
        v.bank_account, v.ifsc, v.bank_verified = v.pending_bank_account, v.pending_ifsc, True
    v.pending_bank_account = v.pending_ifsc = v.pending_at = None
    log(db, user, "BANK_VERIFIED" if approve else "BANK_REJECTED", f"Bank details of {v.name} {'verified' if approve else 'rejected'}")
    for u in db.scalars(select(AppUser).where(AppUser.vendor_id == v.id)):
        notify(db, u.id, f"Bank details {'verified' if approve else 'rejected'}", f"Accounts has {'verified' if approve else 'rejected'} your bank details.")
    db.commit()
    return v


def pay_block(db, d):
    if d.doc_type != "INVOICE" or not d.vendor_id:
        return None
    v = db.get(Vendor, d.vendor_id)
    if v.pending_bank_account:
        return "The vendor changed its bank details and Accounts has not verified them yet"
    if not v.bank_verified or not v.bank_account:
        return "The vendor's bank details are not verified"
    return None


# ---------- documents ----------

def validate_parent(db, company_id, doc_type, parent_id):
    required = {"IOM": "ION", "PRPO": "IOM", "INVOICE": "PRPO"}.get(doc_type)
    optional = {"TRN": "IOM"}.get(doc_type)
    want = required or optional
    if want is None:
        if parent_id:
            err(400, f"{doc_type} cannot be linked to another document")
        return None
    if parent_id is None:
        if required:
            err(400, f"{doc_type} must be linked to an approved {required}")
        return None
    p = db.get(Document, parent_id)
    if not p or (company_id and p.company_id != company_id) or p.doc_type != want:
        err(400, f"Parent must be an {want} from the same company")
    if p.status not in ("APPROVED", "PAID"):
        err(400, f"{ref(p)} is not approved yet")
    return p


def clean_data(db, company_id, doc_type, data):
    allowed_keys = {f["key"] for f in fields_for(db, company_id, doc_type)}
    return {k: v for k, v in (data or {}).items() if k in allowed_keys}


def create_document(db, user, doc_type, title, amount, data, parent_id, vendor_id=None):
    if doc_type not in TEMPLATES:
        err(400, "Unknown document type")
    vendor = None
    if doc_type == "INVOICE":
        if user.role != "vendor":
            err(403, "Invoices are submitted by the vendor, not by company staff")
        parent = validate_parent(db, None, doc_type, parent_id)
        po_rec = sap_rec(db, parent, "PR_PO")
        if parent.vendor_id != user.vendor_id or not (po_rec and po_rec.po_number):
            err(403, "That PO is not addressed to you")
        company_id, vendor = parent.company_id, db.get(Vendor, user.vendor_id)
    else:
        if user.role != "creator":
            err(403, "Only requesters can create this document")
        company_id = user.company_id
        parent = validate_parent(db, company_id, doc_type, parent_id)
        if doc_type == "PRPO":
            if db.scalar(select(Document.id).where(Document.parent_id == parent_id, Document.doc_type == "PRPO")):
                err(400, f"{ref(parent)} already has a PR/PO request")
            vendor = db.get(Vendor, vendor_id) if vendor_id else None
            if not vendor:
                err(400, "Choose the vendor this PO is addressed to")
            if not amount:
                amount = parent.amount
    data = clean_data(db, company_id, doc_type, data)
    if doc_type == "INVOICE":
        data["po_number"] = po_rec.po_number
    d = Document(company_id=company_id, doc_type=doc_type, parent_id=parent_id, vendor_id=vendor.id if vendor else None,
                 created_by=user.id, title=(title or "").strip() or f"New {doc_type}", status="DRAFT", amount=amount or 0,
                 data=data, created_at=now(), updated_at=now())
    db.add(d)
    db.flush()
    log(db, user, "CREATE", f"{ref(d)} created as draft", d)
    db.commit()
    return d


def update_document(db, user, d, title, amount, data):
    if user.id != d.created_by:
        err(403, "Only the person who created this document can edit it")
    if d.status not in EDITABLE:
        err(400, "This document is locked while it is under approval")
    if title is not None:
        d.title = title.strip() or d.title
    if amount is not None:
        if d.doc_type == "INVOICE" and abs(amount - d.amount) > 0.005 and steps_of(db, d):
            d.data = {**d.data, "reset_approvals": True}  # earlier approvals were given for a different amount
        d.amount = amount
    if data:
        keep = {k: d.data[k] for k in ("po_number", "attachments", "received_value", "ses_gr_no", "reset_approvals") if k in d.data}
        d.data = {**d.data, **clean_data(db, d.company_id, d.doc_type, data), **keep}
    d.updated_at = now()
    log(db, user, "EDIT", f"{ref(d)} edited", d)
    db.commit()


def po_summary(db, d):
    """PO value, amount committed by other invoices on the same PO, what is left, and the three-way match numbers."""
    po = db.get(Document, d.parent_id)
    others = db.scalars(select(Document).where(
        Document.parent_id == po.id, Document.doc_type == "INVOICE", Document.id != d.id, Document.status.in_(ACTIVE))).all()
    used = sum(x.amount for x in others)
    prev_received = max([float(x.data.get("received_value") or 0) for x in others if x.status in ("APPROVED", "PAID")] or [0])
    return {"po_value": po.amount, "invoiced_others": used, "remaining": po.amount - used,
            "this_pct": round(d.amount / po.amount * 100, 1) if po.amount else 0,
            "after_pct": round((used + d.amount) / po.amount * 100, 1) if po.amount else 0,
            "received_before": prev_received, "tolerance_pct": MATCH_TOLERANCE_PCT,
            "received_this": d.data.get("received_value")}


def validate_invoice(db, d):
    vendor = db.get(Vendor, d.vendor_id)
    try:
        rate = float(d.data.get("gst_rate"))
    except (TypeError, ValueError):
        err(400, "Choose a GST rate")
    if rate not in GST_RATES:
        err(400, "GST rate must be one of 0, 5, 12, 18, 28")
    try:
        pct = float(d.data.get("percent_payable"))
    except (TypeError, ValueError):
        err(400, "Percent payable must be a number")
    if not 0 < pct <= 100:
        err(400, "Percent payable must be between 1 and 100")
    try:
        idate = dt.date.fromisoformat(str(d.data.get("invoice_date")))
    except ValueError:
        err(400, "Invoice date must be a valid date")
    if idate > dt.date.today() + dt.timedelta(days=1):
        err(400, "Invoice date cannot be in the future")
    irn = str(d.data.get("irn") or "").strip()
    if irn and not IRN_RE.match(irn):
        err(400, "IRN must be 64 hexadecimal characters")
    if vendor.einvoice_required and not irn:
        err(400, "Your company is under e-invoicing, so the IRN is mandatory on every invoice")
    if not GSTIN_RE.match(vendor.gstin):
        err(400, "The vendor GSTIN on file is not valid, ask the admin to correct it")
    n = norm_inv(d.data.get("invoice_no"))
    for x in db.scalars(select(Document).where(Document.doc_type == "INVOICE", Document.vendor_id == d.vendor_id,
                                               Document.id != d.id, Document.status.in_(ACTIVE))):
        if norm_inv(x.data.get("invoice_no")) == n:
            err(400, f"Invoice number {d.data.get('invoice_no')} was already submitted by you ({ref(x)}). Duplicate invoices are not accepted")
    ps = po_summary(db, d)
    if d.amount > ps["remaining"] + 0.005:
        err(400, f"Invoice exceeds the PO value: PO {ps['po_value']:,.0f}, already invoiced {ps['invoiced_others']:,.0f}, "
                 f"left {ps['remaining']:,.0f}, this invoice {d.amount:,.0f}")


def build_steps(db, d, cfg):
    for c in cfg:  # snapshot the chain so later config changes don't touch documents in flight
        db.add(ApprovalStep(document_id=d.id, level=c.level, label=c.label, kind=c.kind,
                            approver_id=resolve_approver(db, c, d), decision="pending"))


def submit(db, user, d):
    if user.id != d.created_by:
        err(403, "Only the person who created this document can submit it")
    if d.status != "DRAFT":
        err(400, "Only drafts can be submitted")
    missing = [f["label"] for f in fields_for(db, d.company_id, d.doc_type)
               if not f.get("optional") and not str(d.data.get(f["key"], "")).strip()]
    if missing:
        err(400, "Fill in: " + ", ".join(missing))
    if not d.amount or d.amount <= 0:
        err(400, "Enter an amount greater than zero")
    if d.doc_type == "INVOICE":
        validate_invoice(db, d)
    cfg, skipped = cfg_for(db, d)
    if not cfg:
        err(400, f"No approvers configured for {d.doc_type}")
    build_steps(db, d, cfg)
    db.flush()
    d.status, d.current_level, d.updated_at = "PENDING", cfg[0].level, now()
    if d.doc_type == "PRPO" and not sap_rec(db, d, "PR_PO"):  # SAP raises the PR at once, the PO only after approval
        rec = SapRecord(document_id=d.id, kind="PR_PO", created_by_name=user.name, created_at=now())
        db.add(rec)
        db.flush()
        rec.pr_number = f"10{rec.id:08d}"
        log(db, user, "SAP_PR", f"Mock SAP raised PR {rec.pr_number} for {ref(d)}", d)
    log(db, user, "SUBMIT", f"{ref(d)} submitted, {len(cfg)} approval level(s)"
        + (f", level(s) {', '.join(str(c.level) for c in skipped)} skipped as the amount is below their limit" if skipped else ""), d)
    first = current_step(db, d)
    notify_approver(db, first.approver_id, f"Approval needed: {ref(d)} {d.title}", f"{ref(d)} is waiting for you ({first.label}, level {first.level}).", d)
    db.commit()


def resubmit(db, user, d):
    if user.id != d.created_by:
        err(403, "Only the person who created this document can resubmit it")
    if d.status not in ("QUERY", "REJECTED"):
        err(400, "Nothing to resubmit")
    if d.doc_type == "INVOICE":
        validate_invoice(db, d)
    reset = d.data.get("reset_approvals")
    if reset:  # amount changed after some approvals: everyone, the requester's work confirmation included, signs off again
        d.data = {k: v for k, v in d.data.items() if k not in ("reset_approvals", "received_value", "ses_gr_no")}
        for x in steps_of(db, d):
            x.decision, x.acted_at, x.acted_by, x.comment = "pending", None, None, ""
        db.flush()
    cfg, _ = cfg_for(db, d)  # the amount may have changed while editing, so re-check which levels apply
    have = {x.level: x for x in steps_of(db, d)}
    need = {c.level: c for c in cfg}
    for lvl, c in need.items():
        if lvl not in have:
            db.add(ApprovalStep(document_id=d.id, level=lvl, label=c.label, kind=c.kind,
                                approver_id=resolve_approver(db, c, d), decision="pending"))
    for lvl, x in have.items():
        if lvl not in need and x.decision == "pending":
            db.delete(x)
    s = current_step(db, d)
    s.decision, s.acted_at, s.acted_by = "pending", None, None
    db.flush()
    d.current_level = min(x.level for x in steps_of(db, d) if x.decision == "pending")
    s = current_step(db, d)
    d.status, d.updated_at = "PENDING", now()
    log(db, user, "RESUBMIT", f"{ref(d)} resubmitted to {name(db, s.approver_id)} (level {s.level})"
        + (", amount changed so all approvals restart" if reset else ""), d)
    notify_approver(db, s.approver_id, f"Resubmitted: {ref(d)} {d.title}", f"{user.name} has made changes to {ref(d)}. Please review again.", d)
    db.commit()


def nudge(db, user, d):
    if user.id != d.created_by:
        err(403, "Only the person who created this document can send a reminder")
    if d.status != "PENDING":
        err(400, "Only documents in approval can be reminded")
    if db.scalar(select(AuditLog.id).where(AuditLog.document_id == d.id, AuditLog.action == "NUDGE",
                                           AuditLog.at > now() - dt.timedelta(hours=1))):
        err(400, "You already sent a reminder in the last hour")
    s = current_step(db, d)
    days = (now() - d.updated_at).total_seconds() / 86400
    notify_approver(db, s.approver_id, f"Reminder: {ref(d)} {d.title}",
                    f"{user.name} is waiting on your approval of {ref(d)} (level {s.level}), pending for {days:.1f} day(s).", d)
    log(db, user, "NUDGE", f"Reminded {name(db, s.approver_id)} about {ref(d)}", d)
    db.commit()


def three_way(db, d, received):
    """Requester confirms how much has been delivered. Invoiced to date must not outrun received to date, within tolerance."""
    try:
        received = float(received)
    except (TypeError, ValueError):
        err(400, "Enter the value received to date (before GST) to confirm the work")
    if received < 0:
        err(400, "Value received cannot be negative")
    ps = po_summary(db, d)
    tol = 1 + MATCH_TOLERANCE_PCT / 100
    if received > ps["po_value"] * tol + 0.005:
        err(400, f"Value received {received:,.0f} is more than the PO value {ps['po_value']:,.0f}")
    if received + 0.005 < ps["received_before"]:
        err(400, f"Value received to date cannot be lower than {ps['received_before']:,.0f}, already confirmed on an earlier invoice")
    invoiced = ps["invoiced_others"] + d.amount
    if invoiced > received * tol + 0.005:
        err(400, f"Three-way match failed: invoiced to date {invoiced:,.0f} is more than received to date {received:,.0f} "
                 f"(tolerance {MATCH_TOLERANCE_PCT:g}%). Raise a query to the vendor instead of approving")
    return received


def act(db, user, d, decision, comment, received_value=None, ses_gr_no=None):
    if decision not in ("approve", "reject", "query"):
        err(400, "Decision must be approve, reject or query")
    if d.status != "PENDING":
        err(400, "This document is not waiting for approval")
    s = current_step(db, d)
    if not (s.approver_id == user.id or delegate_of(db, s.approver_id) == user.id):
        err(403, f"Waiting on {name(db, s.approver_id)}, not you")
    reason = sod_reason(db, user, d, s)
    if reason:
        err(403, reason)
    comment = (comment or "").strip()
    if decision != "approve" and not comment:
        err(400, "Add a comment so the sender knows what to fix")
    if decision == "approve" and s.kind == "requester" and d.doc_type == "INVOICE":
        ses = (ses_gr_no or "").strip()
        if not ses:
            err(400, "Enter the service entry sheet or goods receipt number, Accounts needs it to process payment")
        d.data = {**d.data, "received_value": three_way(db, d, received_value), "ses_gr_no": ses[:40]}
    s.decision = {"approve": "approved", "reject": "rejected", "query": "query"}[decision]
    s.comment, s.acted_at, s.acted_by, d.updated_at = comment, now(), user.id, now()
    if comment:
        db.add(Comment(document_id=d.id, user_id=user.id, text=comment, created_at=now()))
    if decision == "approve":
        later = [x for x in steps_of(db, d) if x.level > s.level]
        if later:
            d.current_level = later[0].level
            notify_approver(db, later[0].approver_id, f"Approval needed: {ref(d)} {d.title}",
                            f"{ref(d)} cleared {s.label} and is waiting for you ({later[0].label}, level {later[0].level}).", d)
        else:
            d.status = "APPROVED"
            msg = f"{ref(d)} has cleared every approval level."
            if d.doc_type == "PRPO":
                rec = sap_rec(db, d, "PR_PO")
                rec.po_number, rec.approved_by_name = f"45{rec.id:08d}", user.name
                log(db, user, "SAP_PO", f"Mock SAP issued PO {rec.po_number} for {ref(d)}", d)
                msg += f" SAP issued PO {rec.po_number}."
                for vu in db.scalars(select(AppUser).where(AppUser.vendor_id == d.vendor_id)):
                    notify(db, vu.id, f"New purchase order {rec.po_number}",
                           f"{db.get(Company, d.company_id).code} has issued PO {rec.po_number} to you ({d.title}). You can now submit invoices against it.", d)
            if d.doc_type == "INVOICE":
                msg += f" Service entry or goods receipt: {d.data.get('ses_gr_no', 'not recorded')}."
                for a in db.scalars(select(AppUser).where(AppUser.company_id == d.company_id, AppUser.role == "accounts")):
                    notify(db, a.id, f"Ready to post: {ref(d)}", f"{ref(d)} is fully approved. Post it to SAP and release payment.", d)
            notify(db, d.created_by, f"Approved: {ref(d)} {d.title}", msg, d)
    else:
        d.status = "QUERY" if decision == "query" else "REJECTED"
        notify(db, d.created_by, f"{'Query' if decision == 'query' else 'Rejected'}: {ref(d)} {d.title}",
               f"{user.name} ({s.label}, level {s.level}) says: {comment}\nPlease update the document and resubmit.", d)
    log(db, user, decision.upper(), f"{ref(d)} {s.label}, level {s.level}" + (f" on behalf of {name(db, s.approver_id)}" if s.approver_id != user.id else "")
        + (f", received to date {d.data['received_value']:,.0f}, SES/GR {d.data['ses_gr_no']}" if decision == "approve" and s.kind == "requester" and d.doc_type == "INVOICE" else "")
        + (f": {comment}" if comment else ""), d)
    db.commit()


# ---------- mock SAP and payment ----------

def accounts_guard(db, user, d):
    if user.role != "accounts" or user.company_id != d.company_id:
        err(403, "Only Accounts of this company can do this")
    po = db.get(Document, d.parent_id)
    if user.id == po.created_by:
        err(403, "You raised the PO for this invoice, so you cannot post or pay it")


def post_invoice(db, user, d, park_user="", ses_gr_no=""):
    """Accounts records who parked the invoice in SAP (an internal person, never the vendor) and who posted it."""
    if d.doc_type != "INVOICE" or d.status != "APPROVED":
        err(400, "Only an approved invoice can be posted to SAP")
    accounts_guard(db, user, d)
    if sap_rec(db, d, "INVOICE_POST"):
        err(400, "Already posted")
    park_user = (park_user or "").strip()[:100]
    if not park_user:
        err(400, "Enter the SAP park user, the person who parked this invoice in SAP")
    if not d.data.get("ses_gr_no"):  # the chain had no requester step, so Accounts records the document handed over
        ses = (ses_gr_no or "").strip()[:40]
        if not ses:
            err(400, "Enter the service entry sheet or goods receipt number")
        d.data = {**d.data, "ses_gr_no": ses}
    n = (db.scalar(select(func.count(SapRecord.id))) or 0) + 1
    rec = SapRecord(document_id=d.id, kind="INVOICE_POST", sap_doc_no=f"51{n:08d}", park_user=park_user,
                    post_user=user.name, created_at=now())
    db.add(rec)
    log(db, user, "SAP_POST", f"Invoice {ref(d)} parked by {rec.park_user}, posted by {rec.post_user}, SAP doc {rec.sap_doc_no}, SES/GR {d.data['ses_gr_no']}", d)
    db.commit()


def pay(db, user, d):
    if d.doc_type != "INVOICE" or d.status != "APPROVED" or not sap_rec(db, d, "INVOICE_POST"):
        err(400, "Post the approved invoice to SAP before releasing payment")
    accounts_guard(db, user, d)
    blocked = pay_block(db, d)
    if blocked:
        err(400, "Payment blocked: " + blocked)
    t = tax(d)
    amt = t["payable"]
    p = Payment(invoice_id=d.id, amount=amt, reference=f"PAY{d.id:06d}", paid_by=user.name, paid_at=now())
    db.add(p)
    d.status, d.updated_at = "PAID", now()
    log(db, user, "PAYMENT", f"{ref(d)} paid INR {amt:,.2f} ({t['percent_payable']:g}% of {t['total']:,.2f} incl. GST), ref {p.reference}", d)
    po = db.get(Document, d.parent_id)
    notify(db, po.created_by, f"Paid: {ref(d)} {d.title}", f"Payment of INR {amt:,.2f} released, ref {p.reference}.", d)
    v = db.get(Vendor, d.vendor_id)
    notify(db, d.created_by, f"Payment advice: invoice {d.data.get('invoice_no', '')}",
           f"Dear {v.name},\nPayment of INR {amt:,.2f} against PO {d.data.get('po_number')} and invoice {d.data.get('invoice_no')} has been released (ref {p.reference}).",
           d, to_address=v.email)
    db.commit()


# ---------- read side ----------

def allowed(db, user, d):
    if user.role == "auditor" or not can_see(user, d):
        return []
    a, mine = [], user.id == d.created_by
    if mine and d.status in EDITABLE:
        a += ["edit", "attach"]
    if mine and d.status == "DRAFT":
        a.append("submit")
    if mine and d.status in ("QUERY", "REJECTED"):
        a.append("resubmit")
    if mine and d.status == "PENDING":
        a.append("nudge")
    if d.status == "PENDING":
        s = current_step(db, d)
        if can_act(db, user, d, s):
            a += ["approve", "reject", "query"]
    same = user.company_id == d.company_id
    if (d.doc_type == "IOM" and d.status == "APPROVED" and user.role == "creator" and same
            and not db.scalar(select(Document.id).where(Document.parent_id == d.id, Document.doc_type == "PRPO"))):
        a.append("request_prpo")
    if d.doc_type == "INVOICE" and d.status == "APPROVED" and user.role == "accounts" and same \
            and user.id != db.get(Document, d.parent_id).created_by:
        if not sap_rec(db, d, "INVOICE_POST"):
            a.append("post_sap")
        elif not pay_block(db, d):
            a.append("pay")
    return a


def waiting_text(db, d):
    if d.status == "PENDING":
        s = current_step(db, d)
        if not s:
            return None
        w = name(db, s.approver_id)
        dg = delegate_of(db, s.approver_id)
        return w + (f", delegate {name(db, dg)}" if dg else "")
    if d.status in ("QUERY", "REJECTED"):
        return name(db, d.created_by)
    return None


def brief(db, d, user=None):
    age = None
    if d.status in ("PENDING", "QUERY", "REJECTED"):
        age = round((now() - d.updated_at).total_seconds() / 86400, 1)
    waiting = waiting_text(db, d)
    vendor = db.get(Vendor, d.vendor_id).name if d.vendor_id else None
    if user is not None and user.role == "vendor" and d.status == "PENDING":
        waiting = "Under review"  # vendors see status, not who internally is holding it
    return {"id": d.id, "ref": ref(d), "company": db.get(Company, d.company_id).code, "company_id": d.company_id, "doc_type": d.doc_type,
            "title": d.title, "status": d.status, "amount": d.amount, "created_by": name(db, d.created_by), "vendor": vendor,
            "waiting_on": waiting, "age_days": age, "stuck": age is not None and age >= STUCK_DAYS,
            "updated_at": d.updated_at.isoformat(), "parent_id": d.parent_id}


def detail(db, user, d):
    out = brief(db, d, user)
    s = current_step(db, d) if d.status == "PENDING" else None
    is_vendor = user.role == "vendor"
    out.update({
        "data": d.data, "current_level": d.current_level, "created_at": d.created_at.isoformat(),
        "amount_label": AMOUNT_LABEL[d.doc_type], "fields": fields_for(db, d.company_id, d.doc_type), "actions": allowed(db, user, d),
        "steps": [{"level": x.level, "label": x.label, "kind": x.kind, "approver": name(db, x.approver_id), "decision": x.decision,
                   "comment": x.comment, "acted_at": x.acted_at.isoformat() if x.acted_at else None,
                   "delegate": name(db, x.acted_by) if x.acted_by and x.acted_by != x.approver_id else None} for x in steps_of(db, d)],
        "chain_info": chain_info(db, d),
        "comments": [{"user": name(db, c.user_id), "text": c.text, "at": c.created_at.isoformat()} for c in
                     db.scalars(select(Comment).where(Comment.document_id == d.id).order_by(Comment.id))],
        "sap": [{k: getattr(r, k) for k in ("kind", "pr_number", "po_number", "sap_doc_no", "created_by_name",
                "approved_by_name", "park_user", "post_user")} | {"at": r.created_at.isoformat()}
                for r in db.scalars(select(SapRecord).where(SapRecord.document_id == d.id))],
        "payment": next(({"amount": p.amount, "reference": p.reference, "paid_by": p.paid_by, "paid_at": p.paid_at.isoformat()}
                         for p in db.scalars(select(Payment).where(Payment.invoice_id == d.id))), None),
        "history": [{"at": a.at.isoformat(), "user": a.username, "action": a.action, "detail": a.detail} for a in
                    db.scalars(select(AuditLog).where(AuditLog.document_id == d.id).order_by(AuditLog.id.desc()))],
        "vendor_info": vendor_out(db.get(Vendor, d.vendor_id)) if d.vendor_id else None,
        "tax": tax(d) if d.doc_type == "INVOICE" else None,
        "blocked_reason": sod_reason(db, user, d, s) if s and (s.approver_id == user.id or delegate_of(db, s.approver_id) == user.id) else None,
        "pay_block": pay_block(db, d) if d.status == "APPROVED" and user.role in ("accounts", "admin", "auditor") else None,
    })
    out["awaiting_requester"] = bool(s and s.kind == "requester" and "approve" in out["actions"])
    fam = [x for x in family(db, d) if can_see(user, x)]
    out["family"] = [brief(db, x, user) for x in fam]
    out["po_summary"] = po_summary(db, d) if d.doc_type == "INVOICE" else None
    if is_vendor:  # vendors see the PO and the state of their invoice, never the internal approval trail
        out["history"] = []
        if d.doc_type == "PRPO":
            out["steps"], out["comments"] = [], []
            out["sap"] = [x for x in out["sap"] if x["kind"] == "PR_PO"]
            for x in out["sap"]:
                x["created_by_name"] = x["approved_by_name"] = None
        else:
            for x in out["steps"]:
                x["approver"] = x["label"]
                x["delegate"] = None
            out["chain_info"] = {"levels": [x["level"] for x in out["steps"]], "skipped": []}
    return out


def chain_info(db, d):
    ok_, skipped = cfg_for(db, d)
    return {"levels": [c.level for c in ok_], "skipped": [{"level": c.level, "from": c.min_amount} for c in skipped]}


def family(db, d):
    """Every document in the same chain, root first, so the page can draw the whole tree."""
    root = d
    while root.parent_id:
        root = db.get(Document, root.parent_id)
    out, stack = [], [root]
    while stack:
        x = stack.pop(0)
        out.append(x)
        stack = list(db.scalars(select(Document).where(Document.parent_id == x.id).order_by(Document.id))) + stack
    return out


def inbox(db, user):
    docs = db.scalars(visible(db, user)).all()
    r = {"to_approve": [], "returned": [], "to_raise_sap": [], "to_pay": [], "stuck": []}
    for d in docs:
        a = allowed(db, user, d)
        if "approve" in a:
            r["to_approve"].append(brief(db, d, user))
        if "resubmit" in a:
            r["returned"].append(brief(db, d, user))
        if "request_prpo" in a:
            r["to_raise_sap"].append(brief(db, d, user))
        if "post_sap" in a or "pay" in a:
            r["to_pay"].append(brief(db, d, user))
    seen = {x["id"] for k in ("to_approve", "returned", "to_raise_sap", "to_pay") for x in r[k]}
    if user.role != "vendor":
        for d in docs:  # visibility for everyone internal: what has been sitting too long
            b = brief(db, d, user)
            if b["stuck"] and b["id"] not in seen:
                r["stuck"].append(b)
        r["stuck"].sort(key=lambda x: -x["age_days"])
    return r


def dashboard(db, user, company_id=None, doc_type=None, since=None, until=None):
    """Numbers for the landing page. Documents are counted by the date they were created."""
    glob = user.role in GLOBAL_ROLES
    q = visible(db, user)
    if glob and company_id:
        q = q.where(Document.company_id == company_id)
    if doc_type:
        q = q.where(Document.doc_type == doc_type)
    if since:
        q = q.where(Document.created_at >= since)
    if until:
        q = q.where(Document.created_at <= until)
    docs = db.scalars(q).all()
    ids = {d.id for d in docs}
    briefs = {d.id: brief(db, d, user) for d in docs}
    pipeline = {t: {} for t in TEMPLATES}
    for d in docs:
        pipeline[d.doc_type][d.status] = pipeline[d.doc_type].get(d.status, 0) + 1
    paid_by_invoice = {p.invoice_id: p.amount for p in db.scalars(select(Payment)) if p.invoice_id in ids}

    wait = {}
    if user.role != "vendor":
        for d in docs:
            if d.status == "PENDING":
                s = current_step(db, d)
                w = wait.setdefault(s.approver_id, {"approver": name(db, s.approver_id), "count": 0, "stuck": 0, "oldest": 0})
                w["count"] += 1
                w["stuck"] += 1 if briefs[d.id]["stuck"] else 0
                w["oldest"] = max(w["oldest"], briefs[d.id]["age_days"] or 0)

    submitted = {}
    for a in db.scalars(select(AuditLog).where(AuditLog.action == "SUBMIT", AuditLog.document_id.in_(ids)).order_by(AuditLog.id)):
        submitted.setdefault(a.document_id, a.at)
    cycle = {t: [] for t in TEMPLATES}
    for d in docs:
        if d.status in ("APPROVED", "PAID") and d.id in submitted:
            last = max((x.acted_at for x in steps_of(db, d) if x.acted_at), default=None)
            if last:
                cycle[d.doc_type].append((last - submitted[d.id]).total_seconds() / 86400)

    recent = []
    if user.role != "vendor":
        rq = select(AuditLog).order_by(AuditLog.id.desc()).limit(8)
        if not glob:
            rq = rq.where(AuditLog.company_id == user.company_id)
        elif company_id:
            rq = rq.where(AuditLog.company_id == company_id)
        if since:
            rq = rq.where(AuditLog.at >= since)
        if until:
            rq = rq.where(AuditLog.at <= until)
        recent = [{"at": a.at.isoformat(), "user": a.username, "action": a.action, "document_id": a.document_id, "detail": a.detail}
                  for a in db.scalars(rq)]

    companies, options = [], []
    if glob:
        options = [{"id": c.id, "code": c.code} for c in db.scalars(select(Company))]
        if not company_id:
            for c in options:
                mine = [d for d in docs if d.company_id == c["id"]]
                companies.append({"id": c["id"], "company": c["code"],
                                  "open": sum(1 for d in mine if d.status in ("PENDING", "QUERY", "REJECTED")),
                                  "stuck": sum(1 for d in mine if briefs[d.id]["stuck"]),
                                  "paid": sum(paid_by_invoice.get(d.id, 0) for d in mine),
                                  "statuses": {st: sum(1 for d in mine if d.status == st) for st in ("DRAFT", "PENDING", "QUERY", "REJECTED", "APPROVED", "PAID")}})
    tasks = inbox(db, user)
    open_ = sum(1 for d in docs if d.status in ("PENDING", "QUERY", "REJECTED"))
    n_stuck = sum(1 for b in briefs.values() if b["stuck"])
    if user.role == "vendor":
        scope = db.get(Vendor, user.vendor_id).name
    elif not glob:
        scope = db.get(Company, user.company_id).code
    else:
        scope = db.get(Company, company_id).code if company_id else "All companies"
    return {
        "scope": scope,
        "company_options": options, "companies": companies,
        "kpi": {"in_approval": sum(1 for d in docs if d.status == "PENDING"), "stuck": n_stuck,
                "open": open_, "on_track": open_ - n_stuck,
                "approved": sum(1 for d in docs if d.status == "APPROVED"), "paid": sum(1 for d in docs if d.status == "PAID"),
                "draft": sum(1 for d in docs if d.status == "DRAFT"),
                "returned": sum(1 for d in docs if d.status in ("QUERY", "REJECTED")),
                "ready_to_pay": sum(1 for d in docs if d.doc_type == "INVOICE" and d.status == "APPROVED"), "total": len(docs)},
        "my_tasks": {k: len(v) for k, v in tasks.items() if k != "stuck"},
        "pipeline": pipeline,
        "money": {"committed": sum(d.amount for d in docs if d.doc_type == "PRPO" and d.status == "APPROVED"),
                  "invoiced": sum(d.amount for d in docs if d.doc_type == "INVOICE" and d.status in ACTIVE),
                  "paid": sum(paid_by_invoice.values())},
        "waiting": sorted(wait.values(), key=lambda w: -w["oldest"]),
        "avg_days": {t: round(sum(v) / len(v), 1) if v else None for t, v in cycle.items()},
        "recent": recent,
    }
