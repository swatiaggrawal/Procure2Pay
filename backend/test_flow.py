"""End to end check of the whole P2P flow with vendors, purchase team, GST and controls. Run: python test_flow.py"""
import datetime as dt
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["UPLOAD_DIR"] = f"{_tmp}/uploads"
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["MAX_UPLOAD_MB"] = "1"
from fastapi.testclient import TestClient
from sqlalchemy import text

import app as appmod
import security
from models import SessionLocal, engine

c = TestClient(appmod.app)
PW = "demo1234"


def login(u, pw=PW):
    r = c.post("/api/login", json={"username": u, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def post(path, h, **body):
    return c.post(path, headers=h, json=body)


def act(d, h, decision="approve", comment="", code=200, **kw):
    return ok(c.post(f"/api/documents/{d['id']}/action", headers=h, json={"decision": decision, "comment": comment, **kw}), code)


cr, a1, a2, ac, pu1, pu2 = (login(f"{n}.tutpl") for n in ["creator", "approver1", "approver2", "accounts", "purchase1", "purchase2"])
adm, aud = login("admin"), login("auditor")
vc, vb = login("vendor.cooltech"), login("vendor.betasec")
vendors = ok(c.get("/api/vendors", headers=cr))
cool = next(v for v in vendors if v["name"].startswith("CoolTech"))

# ---- ION and IOM: approvers, query loop ----
ion = ok(post("/api/documents", cr, doc_type="ION", title="Server room cooling", amount=300000,
              data={"department": "IT", "justification": "Racks overheating", "vendors": "A, B"}))
ok(post(f"/api/documents/{ion['id']}/submit", cr))
act(ion, a2, code=403)                                    # not their turn
act(ion, a1, "query", code=400)                           # comment required
d = act(ion, a1, "query", "Add vendor list")
assert d["status"] == "QUERY"
ok(c.patch(f"/api/documents/{ion['id']}", headers=cr, json={"data": {"vendors": "A, B, C"}}))
d = ok(post(f"/api/documents/{ion['id']}/resubmit", cr))
assert d["status"] == "PENDING" and d["waiting_on"].startswith("Arjun")
act(ion, a1)
assert act(ion, a2)["status"] == "APPROVED"
iom = ok(post("/api/documents", cr, doc_type="IOM", title="Cooling vendor", amount=280000, parent_id=ion["id"],
              data={"quotes": "CoolTech, 280000", "l1_vendor": "CoolTech", "justification": "L1"}))
ok(post(f"/api/documents/{iom['id']}/submit", cr))
act(iom, a1); act(iom, a2)

# ---- PR/PO: needs a vendor, approved by the purchase team, not by business approvers ----
ok(post("/api/documents", cr, doc_type="PRPO", amount=1, parent_id=iom["id"], data={"items": "x", "delivery": "y"}), 400)  # no vendor
prpo = ok(post("/api/documents", cr, doc_type="PRPO", title="Cooling PO", amount=280000, parent_id=iom["id"], vendor_id=cool["id"],
               data={"items": "2 cooling units", "delivery": "Plant, 4 weeks"}))
assert prpo["vendor"].startswith("CoolTech")
ok(post(f"/api/documents/{prpo['id']}/submit", cr))
ok(c.get(f"/api/documents/{prpo['id']}", headers=vc), 404)       # vendor cannot see the PO before it is approved
act(prpo, a1, code=403)                                          # business approver is not on the PO chain
act(prpo, cr, code=403)                                          # requester cannot approve own document
act(prpo, pu1)
d = act(prpo, pu2)
assert d["status"] == "APPROVED"
po_no = next(s["po_number"] for s in d["sap"] if s["po_number"])
assert po_no.startswith("45")

# ---- vendor sees only its own approved PO ----
mine = [x for x in ok(c.get("/api/documents", headers=vc)) if x["company"] == "TUTPL"]
assert [x["doc_type"] for x in mine] == ["PRPO"], mine
assert [x for x in ok(c.get("/api/documents", headers=vb)) if x["company"] == "TUTPL"] == []
ok(c.get(f"/api/documents/{prpo['id']}", headers=vb), 404)
vd = ok(c.get(f"/api/documents/{prpo['id']}", headers=vc))
assert vd["steps"] == [] and vd["history"] == [] and all(f["doc_type"] != "ION" for f in vd["family"])
ok(c.get(f"/api/documents/{ion['id']}", headers=vc), 404)
ok(c.get("/api/audit", headers=vc), 403)
ok(c.get("/api/config", headers=vc), 403)
ok(post("/api/documents", cr, doc_type="INVOICE", amount=1, parent_id=prpo["id"], data={}), 403)   # staff cannot raise invoices
ok(post("/api/documents", vb, doc_type="INVOICE", amount=1, parent_id=prpo["id"], data={}), 403)   # other vendor cannot bill this PO

# ---- invoice by vendor: company fields, GST, IRN, duplicates ----
tpl = ok(c.get(f"/api/templates?company_id={prpo['company_id']}", headers=vc))
assert any(f["key"] == "x_delivery_challan_number" for f in tpl["fields"]["INVOICE"])
ok(c.get(f"/api/templates?company_id={prpo['company_id'] + 1}", headers=vc), 403)
today = dt.date.today().isoformat()
IRN = "b" * 64
base = {"invoice_no": "CT-1001", "invoice_date": today, "hsn_sac": "8415", "gst_rate": 18, "irn": IRN, "payment_terms": "Net 30",
        "percent_payable": 100, "scope_of_work": "Delivered", "work_completed": "60 percent", "x_delivery_challan_number": "DC-77"}
inv = ok(post("/api/documents", vc, doc_type="INVOICE", title="Invoice 1", amount=168000, parent_id=prpo["id"], data={**base, "junk": "dropped"}))
assert "junk" not in inv["data"] and inv["data"]["po_number"] == po_no
assert inv["tax"]["gst"] == 30240 and inv["tax"]["total"] == 198240
sub = lambda d, h=vc, code=200: ok(post(f"/api/documents/{d['id']}/submit", h), code)


def edit(d, h=vc, **data):
    return ok(c.patch(f"/api/documents/{d['id']}", headers=h, json={"data": data}))


edit(inv, x_delivery_challan_number="")
assert "Delivery challan" in sub(inv, code=400)["detail"]            # per-company required field
edit(inv, x_delivery_challan_number="DC-77", gst_rate=7)
assert "GST rate" in sub(inv, code=400)["detail"]
edit(inv, gst_rate=18, irn="")
assert "IRN" in sub(inv, code=400)["detail"]                          # CoolTech is e-invoice applicable
edit(inv, irn="xyz")
assert "64" in sub(inv, code=400)["detail"]
edit(inv, irn=IRN, invoice_date=(dt.date.today() + dt.timedelta(days=30)).isoformat())
assert "future" in sub(inv, code=400)["detail"]
edit(inv, invoice_date=today)
ok(c.patch(f"/api/documents/{inv['id']}", headers=vc, json={"amount": 300000}))
assert "exceeds the PO value" in sub(inv, code=400)["detail"]         # over-billing against the PO (before GST)
ok(c.patch(f"/api/documents/{inv['id']}", headers=vc, json={"amount": 168000}))
ok(c.patch(f"/api/documents/{inv['id']}", headers=cr, json={"title": "x"}), 403)
d = sub(inv)
assert d["status"] == "PENDING" and [s["label"] for s in d["steps"]][:2] == ["Accounts check", "Requester confirms work"]
assert d["waiting_on"] == "Under review"                              # vendor does not see internal names
dup = ok(post("/api/documents", vc, doc_type="INVOICE", title="Dup", amount=1000, parent_id=prpo["id"], data={**base, "invoice_no": " ct-1001 "}))
assert "Duplicate" in sub(dup, code=400)["detail"]

# ---- approval order, segregation of duties, three-way match ----
ok(c.post(f"/api/documents/{inv['id']}/action", headers=a1, json={"decision": "approve"}), 403)     # not their step yet
act(inv, ac, "query", code=400)
d = act(inv, ac, "query", "HSN code looks wrong")
assert d["status"] == "QUERY"
mail = ok(c.get("/api/notifications", headers=vc))
assert any("HSN" in n["body"] for n in mail)                          # query goes to the vendor
edit(inv, hsn_sac="8418")
d = ok(post(f"/api/documents/{inv['id']}/resubmit", vc))
assert d["status"] == "PENDING"
act(inv, ac)
# requester step: only the PO raiser, must confirm received value
act(inv, a1, code=403)
assert ok(c.get(f"/api/documents/{inv['id']}", headers=cr))["awaiting_requester"] is True
act(inv, cr, code=400)                                                # no received value
act(inv, cr, code=400, received_value=170000)                         # no SES/GR number
r = act(inv, cr, code=400, received_value=100000, ses_gr_no="SES-9")                     # invoiced 168000 > received 100000
assert "match" in r["detail"].lower()
r = act(inv, cr, code=400, received_value=900000, ses_gr_no="SES-9")                     # more than the PO
d = act(inv, cr, received_value=170000, ses_gr_no="SES-9")
assert d["data"]["received_value"] == 170000 and d["data"]["ses_gr_no"] == "SES-9"
act(inv, a1)                                                          # Finance approval L3, (level 4 needs >= 250000, skipped)
d = ok(c.get(f"/api/documents/{inv['id']}", headers=ac))
assert d["status"] == "APPROVED", d["status"]

# ---- posting and payment controls ----
assert "post_sap" in d["actions"] and "pay" not in d["actions"]
ok(post(f"/api/documents/{inv['id']}/pay", ac), 400)                  # must post first
ok(post(f"/api/documents/{inv['id']}/sap/post", cr, park_user="X"), 403)   # requester cannot post
ok(post(f"/api/documents/{inv['id']}/sap/post", ac), 400)               # park user required
d = ok(post(f"/api/documents/{inv['id']}/sap/post", ac, park_user="Pooja Nair"))
assert next(s for s in d["sap"] if s["kind"] == "INVOICE_POST")["park_user"] == "Pooja Nair"   # internal person, not the vendor
# vendor changes bank details -> pay blocked until Accounts verifies
b = ok(c.put("/api/vendors/me/bank", headers=vc, json={"account": "12345678901", "ifsc": "SBIN0001234"}))
assert b["bank_pending"] and "Accounts" in ok(post(f"/api/documents/{inv['id']}/pay", ac), 400)["detail"] + "Accounts"
ok(post(f"/api/vendors/{cool['id']}/bank/verify", cr, approve=True), 403)
ok(c.put("/api/vendors/me/bank", headers=vc, json={"account": "12", "ifsc": "bad"}), 400)
ok(post(f"/api/vendors/{cool['id']}/bank/verify", ac, approve=True))
d = ok(post(f"/api/documents/{inv['id']}/pay", ac))
assert d["status"] == "PAID" and d["payment"]["amount"] == 198240     # taxable + GST, full amount
vm = ok(c.get("/api/notifications", headers=vc))
assert any("Payment advice" in n["subject"] for n in vm)
vi = ok(c.get(f"/api/documents/{inv['id']}", headers=vc))
assert vi["payment"] and vi["history"] == []

# ---- the sum of invoices can never exceed the PO ----
inv2 = ok(post("/api/documents", vc, doc_type="INVOICE", title="Invoice 2", amount=115000, parent_id=prpo["id"], data={**base, "invoice_no": "CT-1002"}))
assert "exceeds the PO value" in sub(inv2, code=400)["detail"]       # 168000 + 115000 > 280000
ok(c.patch(f"/api/documents/{inv2['id']}", headers=vc, json={"amount": 112000}))
sub(inv2)
# amount changed after approvals: everything restarts, including the requester's confirmation
act(inv2, ac); act(inv2, cr, received_value=280000, ses_gr_no="SES-10")
act(inv2, a1, "query", "Please recheck the amount")
ok(c.patch(f"/api/documents/{inv2['id']}", headers=vc, json={"amount": 111000}))
d = ok(post(f"/api/documents/{inv2['id']}/resubmit", vc))
assert d["current_level"] == 1 and all(s["decision"] == "pending" for s in d["steps"]) and "received_value" not in d["data"], d
assert d["waiting_on"] == "Under review"

# ---- bank rejection path and amount-based second finance level ----
ok(c.put("/api/vendors/me/bank", headers=vb, json={"account": "998877665544", "ifsc": "ICIC0004321"}))
bet = next(v for v in ok(c.get("/api/vendors", headers=ac)) if v["name"].startswith("BetaSec"))
assert ok(post(f"/api/vendors/{bet['id']}/bank/verify", ac, approve=False))["bank_pending"] is False

# ---- attachments: type, magic bytes, size, download headers ----
draft = ok(post("/api/documents", vc, doc_type="INVOICE", title="Invoice 3", amount=1000, parent_id=prpo["id"], data={**base, "invoice_no": "CT-1003"}))
up = lambda name, raw, h=vc: c.post(f"/api/documents/{draft['id']}/attachment", headers=h, files={"file": (name, raw)})
ok(up("evil.exe", b"MZ..."), 400)
ok(up("fake.pdf", b"not a pdf"), 400)
ok(up("empty.pdf", b""), 400)
ok(up("big.pdf", b"%PDF" + b"0" * (1024 * 1024 + 10)), 413)
ok(up("scan.pdf", b"%PDF-1.4 hello", h=vb), 404)
d = ok(up("../../scan 1.pdf", b"%PDF-1.4 hello"))
fname = d["data"]["attachments"][0]
assert "/" not in fname and fname.endswith(".pdf")
r = c.get(f"/api/documents/{draft['id']}/attachments/{fname}", headers=vc)
assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment") and r.headers["x-content-type-options"] == "nosniff"
ok(c.get(f"/api/documents/{draft['id']}/attachments/{fname}", headers=vb), 404)
for i in range(9):
    ok(up(f"f{i}.csv", b"a,b\n1,2\n"))
ok(up("one-too-many.csv", b"a,b\n"), 400)

# ---- roles: auditor is read only, sees everything ----
assert len(ok(c.get("/api/documents", headers=aud))) >= 5
ok(post("/api/documents", aud, doc_type="ION", amount=1, data={}), 403)
ok(post(f"/api/documents/{iom['id']}/comment", aud, text="hi"), 403)
assert ok(c.get("/api/audit", headers=aud))["total"] > 10
assert ok(c.get("/api/dashboard", headers=aud))["scope"] == "All companies"
dv = ok(c.get("/api/dashboard", headers=vc))
assert dv["scope"].startswith("CoolTech") and dv["recent"] == [] and dv["company_options"] == []
# other companies stay separate for company staff
other = login("creator.trpl")
ok(c.get(f"/api/documents/{prpo['id']}", headers=other), 404)

# ---- admin config: chain with requester step, role checks, extra fields ----
cfg = ok(c.get("/api/config?company_id=1", headers=adm))
assert [l["label"] for l in cfg["chains"]["INVOICE"]][:2] == ["Accounts check", "Requester confirms work"]
lv = lambda **k: {"label": "L", "kind": "fixed", "min_amount": 0, **k}
ok(c.put("/api/config", headers=cr, json={"company_id": 1, "doc_type": "ION", "levels": [lv(approver_id=2)]}), 403)
ok(c.put("/api/config", headers=adm, json={"company_id": 1, "doc_type": "PRPO", "levels": [lv(approver_id=2)]}), 400)  # approver on PRPO chain
ok(c.put("/api/config", headers=adm, json={"company_id": 1, "doc_type": "ION", "levels": [lv(kind="requester")]}), 400)
people = {p["name"]: p["id"] for p in cfg["people"]}
ok(c.put("/api/config", headers=adm, json={"company_id": 1, "doc_type": "TRN", "levels": [lv(approver_id=people["Arjun Mehta (TUTPL)"]), lv(approver_id=people["Arjun Mehta (TUTPL)"], min_amount=5)]}), 400)  # same person twice
ok(c.put("/api/config", headers=adm, json={"company_id": 1, "doc_type": "TRN", "levels": [lv(approver_id=people["Arjun Mehta (TUTPL)"]), lv(approver_id=people["Neha Kapoor (TUTPL)"], min_amount=50000)]}))
r = ok(c.put("/api/field-config", headers=adm, json={"company_id": 1, "doc_type": "INVOICE", "fields": [
    {"label": "Delivery challan number", "ftype": "text", "required": True}, {"label": "Plant code", "ftype": "text", "required": False}]}))
assert [f["label"] for f in r["extra_fields"]["INVOICE"]] == ["Delivery challan number", "Plant code"]
ok(c.put("/api/field-config", headers=cr, json={"company_id": 1, "doc_type": "INVOICE", "fields": []}), 403)
ok(c.put("/api/field-config", headers=adm, json={"company_id": 1, "doc_type": "INVOICE", "fields": [{"label": "x", "ftype": "weird"}]}), 400)
# vendor creation by admin only, GSTIN format checked
nv = dict(name="Gamma Cams", gstin="33AAHCG1111A1Z9", email="g@g.example", username="vendor.gamma", password="longenough1")
ok(post("/api/vendors", cr, **nv), 403)
ok(post("/api/vendors", adm, **{**nv, "gstin": "BADGSTIN"}), 400)
ok(post("/api/vendors", adm, **{**nv, "password": "short"}), 400)
ok(post("/api/vendors", adm, **nv))
login("vendor.gamma", "longenough1")

# ---- audit trail is append only, even with raw SQL ----
with engine.begin() as cx:
    n = cx.execute(text("select count(*) from audit_log")).scalar()
for stmt in ("update audit_log set detail='tampered' where id=1", "delete from audit_log where id=1", "delete from audit_log"):
    try:
        with engine.begin() as cx:
            cx.execute(text(stmt))
        raise SystemExit("audit log was modified: " + stmt)
    except Exception as e:
        assert "append-only" in str(e), e
with engine.begin() as cx:
    assert cx.execute(text("select count(*) from audit_log")).scalar() == n

# ---- passwords and sessions ----
with SessionLocal() as db:
    from models import AppUser
    from sqlalchemy import select
    h = db.scalar(select(AppUser.password_hash).where(AppUser.username == "creator.tutpl"))
    assert h.startswith("$2") and len(h) == 60                          # bcrypt
assert c.post("/api/login", json={"username": "creator.tutpl", "password": "wrong"}).status_code == 401
assert c.post("/api/login", json={"username": "nobody", "password": "wrong"}).status_code == 401
for _ in range(8):   # no lockout: repeated wrong guesses never block the real user
    c.post("/api/login", json={"username": "creator.tutpl", "password": "x"})
login("creator.tutpl")
assert c.get("/api/me").status_code == 401
assert c.get("/api/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401
t1, t2 = login("approver3.tutpl"), login("approver3.tutpl")             # two devices at once
ok(c.get("/api/me", headers=t1)); ok(c.get("/api/me", headers=t2))
ok(post("/api/logout", t1))
assert c.get("/api/me", headers=t1).status_code == 401 and c.get("/api/me", headers=t2).status_code == 200
ok(post("/api/password", t2, current="nope", new="brandnewpass1"), 400)
ok(post("/api/password", t2, current=PW, new="short"), 400)
ok(post("/api/password", t2, current=PW, new="brandnewpass1"))
login("approver3.tutpl", "brandnewpass1")
assert c.post("/api/login", json={"username": "approver3.tutpl", "password": PW}).status_code == 401
# session expiry (absolute and idle)
t3 = login("approver2.trpl")
with engine.begin() as cx:
    cx.execute(text("update user_session set last_seen = :t"), {"t": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=security.IDLE_HOURS + 1)})
assert c.get("/api/me", headers=t3).status_code == 401
t4 = login("approver2.trpl")
with engine.begin() as cx:
    cx.execute(text("update user_session set expires_at = :t"), {"t": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=1)})
assert c.get("/api/me", headers=t4).status_code == 401

# ---- public endpoint, PDFs, audit export ----
vc, adm = login("vendor.cooltech"), login("admin")   # the expiry checks above ended every session
pub = ok(c.get("/api/public"))
assert pub["demo_mode"] and any(u["username"] == "vendor.cooltech" for u in pub["users"])
r = c.get(f"/api/documents/{inv['id']}/pdf", headers=vc)
assert r.status_code == 200 and r.content[:4] == b"%PDF"
r = c.get(f"/api/documents/{prpo['id']}/record.pdf", headers=vc)
assert r.status_code == 200 and r.content[:4] == b"%PDF"
r = c.get("/api/audit/export", headers=adm)
assert r.status_code == 200 and "LOGIN" in r.text
print("ALL CHECKS PASSED")
