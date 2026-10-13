import datetime as dt

from sqlalchemy import select

import security
import service as svc
from models import *

COMPANIES = ["TUTPL", "TRPL", "UEPL", "HEPL", "DSEPL", "DRL", "PITCMRL", "MRL"]
PEOPLE = [("creator", "Riya Sharma", "creator"), ("approver1", "Arjun Mehta", "approver"), ("approver2", "Neha Kapoor", "approver"),
          ("approver3", "Vikram Rao", "approver"), ("accounts", "Sanjay Gupta", "accounts"),
          ("purchase1", "Pooja Nair", "purchase"), ("purchase2", "Rahul Desai", "purchase")]
PASSWORD = "demo1234"  # shown on the login page only while DEMO_MODE is on
# (label, kind, who, amount from which the level applies). Level 1 always applies.
CHAIN = {
    "ION": [("Approval", "fixed", "approver1", 0), ("Senior approval", "fixed", "approver2", 100000)],
    "IOM": [("Approval", "fixed", "approver1", 0), ("Senior approval", "fixed", "approver2", 100000)],
    "TRN": [("Approval", "fixed", "approver1", 0)],
    "PRPO": [("Purchase team", "fixed", "purchase1", 0), ("Purchase head", "fixed", "purchase2", 100000)],
    "INVOICE": [("Accounts check", "fixed", "accounts", 0), ("Requester confirms work", "requester", None, 0),
                ("Finance approval", "fixed", "approver1", 0), ("Finance head", "fixed", "approver2", 250000)],
}
# Extra invoice fields that differ by company: (label, type, required)
EXTRA_INVOICE_FIELDS = {
    "TUTPL": [("Delivery challan number", "text", True)],
    "UEPL": [("Project code", "text", True)],
    "HEPL": [("Cost centre", "text", True)],
    "DSEPL": [("Contract reference", "text", False)],
    "DRL": [("Batch or lot number", "text", False)],
    "PITCMRL": [("Work order number", "text", False)],
    "MRL": [("Plant code", "text", False)],
}
VENDORS = [  # name, gstin, einvoice, login
    ("CoolTech Systems", "27AABCC1234D1Z5", True, "vendor.cooltech", "accounts@cooltech.example"),
    ("BetaSec Solutions", "29AAGCB5678E1Z2", False, "vendor.betasec", "billing@betasec.example"),
    ("AlphaTech Services", "07AAACA9012F1Z8", False, "vendor.alphatech", "finance@alphatech.example"),
]
IRN = "a" * 64  # demo e-invoice reference number


def users_of(db, code):
    return {u.username.split(".")[0]: u for u in db.scalars(select(AppUser).where(AppUser.company_id == db.scalar(select(Company.id).where(Company.code == code))))}


def run(db):
    if db.scalar(select(Company)):
        return
    pw = security.hash_pw(PASSWORD)
    for code in COMPANIES:
        c = Company(code=code, name=code)
        db.add(c)
        db.flush()
        for uname, person, role in PEOPLE:
            db.add(AppUser(company_id=c.id, username=f"{uname}.{code.lower()}", name=f"{person} ({code})",
                           email=f"{uname}.{code.lower()}@{code.lower()}.example", role=role, password_hash=pw))
        db.flush()
        users = users_of(db, code)
        for dtype, levels in CHAIN.items():
            for lvl, (label, kind, who, limit) in enumerate(levels, 1):
                db.add(ApprovalConfig(company_id=c.id, doc_type=dtype, level=lvl, label=label, kind=kind,
                                      approver_id=users[who].id if who else None, min_amount=limit))
        for i, (label, ftype, required) in enumerate(EXTRA_INVOICE_FIELDS.get(code, [])):
            db.add(FieldConfig(company_id=c.id, doc_type="INVOICE", key=svc.slug(label), label=label, ftype=ftype, required=required, position=i))
    db.add(AppUser(company_id=None, username="admin", name="Portal Admin", email="admin@portal.example", role="admin", password_hash=pw))
    db.add(AppUser(company_id=None, username="auditor", name="Internal Auditor (read only)", email="auditor@portal.example", role="auditor", password_hash=pw))
    for name_, gstin, einv, login, email in VENDORS:
        v = Vendor(name=name_, gstin=gstin, email=email, einvoice_required=einv, bank_account="50100" + str(abs(hash(name_)) % 10**8).zfill(8),
                   ifsc="HDFC0001234", bank_verified=True)
        db.add(v)
        db.flush()
        db.add(AppUser(vendor_id=v.id, username=login, name=name_, email=email, role="vendor", password_hash=pw))
    db.commit()
    demo(db)


def approve_all(db, d, received=None):
    while d.status == "PENDING":
        s = svc.current_step(db, d)
        svc.act(db, db.get(AppUser, s.approver_id), d, "approve", "", received if s.kind == "requester" else None, "SES-4471" if s.kind == "requester" else None)


def vendor_user(db, login):
    return db.scalar(select(AppUser).where(AppUser.username == login))


def invoice(db, vu, po, title, taxable, no, extras, **kw):
    data = {"invoice_no": no, "invoice_date": dt.date.today().isoformat(), "hsn_sac": "8415", "gst_rate": 18, "irn": IRN,
            "payment_terms": "Net 30", "percent_payable": 100, "scope_of_work": "Units delivered and installed",
            "work_completed": "50 percent of the order", **extras, **kw}
    return svc.create_document(db, vu, "INVOICE", title, taxable, data, po.id)


def demo(db):
    """TRPL: an approved ION and an IOM stuck on level 2. HEPL: a full purchase through to a paid invoice, plus one in progress."""
    u = lambda n: db.scalar(select(AppUser).where(AppUser.username == f"{n}.trpl"))
    creator, a1, a2 = u("creator"), u("approver1"), u("approver2")
    ion = svc.create_document(db, creator, "ION", "CCTV upgrade for plant security", 500000, {
        "department": "Security and Admin", "justification": "Existing cameras are end of life and miss two gates.",
        "vendors": "AlphaTech, BetaSec, GammaCam"}, None)
    svc.submit(db, creator, ion)
    svc.act(db, a1, ion, "approve", "")
    svc.act(db, a2, ion, "approve", "Budget fits FY plan.")
    iom = svc.create_document(db, creator, "IOM", "CCTV vendor selection", 455000, {
        "quotes": "AlphaTech, 480000\nBetaSec, 455000\nGammaCam, 510000", "l1_vendor": "BetaSec",
        "justification": "BetaSec is L1 and meets the technical spec."}, ion.id)
    svc.submit(db, creator, iom)
    svc.act(db, a1, iom, "approve", "")
    iom.updated_at = now() - dt.timedelta(days=3)  # so the demo opens with a stuck item
    db.commit()
    amc = svc.create_document(db, creator, "ION", "Annual maintenance contract", 120000, {
        "department": "Facilities", "justification": "Renewal of the lift and HVAC contract.", "vendors": "LiftCare, AirFlow"}, None)
    svc.submit(db, creator, amc)
    svc.act(db, a1, amc, "approve", "")
    svc.create_document(db, creator, "ION", "Canteen refurbishment", 60000, {
        "department": "Admin", "justification": "Tables and counters are worn out.", "vendors": ""}, None)
    full_chain(db, "HEPL")
    # AlphaTech has proposed new bank details, so Accounts has something to verify
    alpha = db.scalar(select(Vendor).where(Vendor.name == "AlphaTech Services"))
    svc.set_bank(db, vendor_user(db, "vendor.alphatech"), "60200123456789", "ICIC0004321")


def full_chain(db, code):
    """One purchase taken from ION to a paid invoice, a second invoice waiting at Accounts, so the demo has money and queues."""
    users = users_of(db, code)
    creator, accounts = users["creator"], users["accounts"]
    cool = db.scalar(select(Vendor).where(Vendor.name == "CoolTech Systems"))
    vu = vendor_user(db, "vendor.cooltech")
    ion = svc.create_document(db, creator, "ION", "Server room cooling", 350000, {
        "department": "IT", "justification": "Racks are overheating in summer.", "vendors": "CoolTech, AirPro"}, None)
    svc.submit(db, creator, ion); approve_all(db, ion)
    iom = svc.create_document(db, creator, "IOM", "Cooling vendor selection", 330000, {
        "quotes": "CoolTech, 330000\nAirPro, 365000", "l1_vendor": "CoolTech", "justification": "L1 and technically compliant."}, ion.id)
    svc.submit(db, creator, iom); approve_all(db, iom)
    prpo = svc.create_document(db, creator, "PRPO", "Cooling PR and PO", 0, {
        "items": "2 precision cooling units", "delivery": "Server room, 4 weeks"}, iom.id, cool.id)
    svc.submit(db, creator, prpo); approve_all(db, prpo)
    extras = {"x_cost_centre": "CC-IT-204"}
    inv = invoice(db, vu, prpo, "Cooling invoice 1 of 2", 165000, "CT-2041", extras)
    svc.submit(db, vu, inv); approve_all(db, inv, received=165000)
    svc.post_invoice(db, accounts, inv, "Pooja Nair (HEPL purchase)")
    svc.pay(db, accounts, inv)
    inv2 = invoice(db, vu, prpo, "Cooling invoice 2 of 2", 165000, "CT-2077", extras, work_completed="Balance 50 percent, commissioned")
    svc.submit(db, vu, inv2)  # waits at the Accounts check
