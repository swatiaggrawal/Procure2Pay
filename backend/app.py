import csv
import datetime as dt
import io
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, inspect, or_, select

import pdfgen
import security
import seed
import service as svc
from models import *

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = Path(os.getenv("UPLOAD_DIR", ROOT / "uploads"))
UPLOADS.mkdir(parents=True, exist_ok=True)
DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"  # shows the quick-login buttons; switch off for anything real
MAX_UPLOAD = int(float(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024)
MAX_FILES = 10
# extension -> signature the file must start with (None means a plain-text type that is checked differently)
SIGNATURES = {".pdf": [b"%PDF"], ".png": [b"\x89PNG\r\n\x1a\n"], ".jpg": [b"\xff\xd8\xff"], ".jpeg": [b"\xff\xd8\xff"],
              ".xlsx": [b"PK\x03\x04"], ".docx": [b"PK\x03\x04"], ".xls": [b"\xd0\xcf\x11\xe0"], ".csv": None}

app = FastAPI(title="Procure to Pay approval portal", docs_url="/api/docs" if DEMO_MODE else None, redoc_url=None, openapi_url="/api/openapi.json" if DEMO_MODE else None)
if os.getenv("CORS_ORIGINS"):
    app.add_middleware(CORSMiddleware, allow_origins=os.environ["CORS_ORIGINS"].split(","), allow_headers=["*"], allow_methods=["*"])


@app.middleware("http")
async def headers(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def prepare_db():
    insp = inspect(engine)
    if "app_user" in insp.get_table_names() and "vendor_id" not in {c["name"] for c in insp.get_columns("app_user")}:
        Base.metadata.drop_all(engine)  # database from an older version of the demo, the schema changed too much to patch
    Base.metadata.create_all(engine)
    install_audit_guards()


prepare_db()
with SessionLocal() as _db:
    seed.run(_db)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def me(authorization: Optional[str] = Header(None), db=Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please log in")
    s = db.scalar(select(UserSession).where(UserSession.token_hash == security.hash_token(authorization[7:])))
    t = now()
    if not s or s.expires_at < t or s.last_seen < t - dt.timedelta(hours=security.IDLE_HOURS):
        if s:
            db.delete(s)
            db.commit()
        raise HTTPException(401, "Session expired, log in again")
    if (t - s.last_seen).total_seconds() > 60:
        s.last_seen = t
        db.commit()
    u = db.get(AppUser, s.user_id)
    u._token_hash = s.token_hash  # so logout knows which session to end
    return u


def doc_for(db, user, doc_id):
    d = db.get(Document, doc_id)
    if not d or not svc.can_see(user, d):  # same answer for "missing" and "not yours", so ids cannot be probed
        raise HTTPException(404, "Document not found")
    return d


def writer(user=Depends(me)):
    if user.role == "auditor":
        raise HTTPException(403, "The auditor account is read-only")
    return user


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    current: str
    new: str


class DocIn(BaseModel):
    doc_type: str
    title: str = ""
    amount: float = 0
    data: dict = {}
    parent_id: Optional[int] = None
    vendor_id: Optional[int] = None


class DocPatch(BaseModel):
    title: Optional[str] = None
    amount: Optional[float] = None
    data: Optional[dict] = None


class ActionIn(BaseModel):
    decision: str
    comment: str = ""
    received_value: Optional[float] = None
    ses_gr_no: str = ""


class CommentIn(BaseModel):
    text: str


class LevelIn(BaseModel):
    label: str = "Approval"
    kind: str = "fixed"
    approver_id: Optional[int] = None
    min_amount: float = 0


class ConfigIn(BaseModel):
    company_id: int
    doc_type: str
    levels: list[LevelIn]


class FieldIn(BaseModel):
    label: str
    ftype: str = "text"
    required: bool = True


class FieldsIn(BaseModel):
    company_id: int
    doc_type: str
    fields: list[FieldIn]


class DelegateIn(BaseModel):
    delegate_id: Optional[int] = None


class VendorIn(BaseModel):
    name: str
    gstin: str
    email: str
    einvoice_required: bool = False
    username: str
    password: str


class BankIn(BaseModel):
    account: str
    ifsc: str


class VerifyIn(BaseModel):
    approve: bool = True


def user_out(db, u):
    co = db.get(Company, u.company_id) if u.company_id else None
    unread = db.scalar(select(func.count(Notification.id)).where(Notification.user_id == u.id, Notification.is_read == False))  # noqa: E712
    v = db.get(Vendor, u.vendor_id) if u.vendor_id else None
    return {"id": u.id, "name": u.name, "username": u.username, "role": u.role, "readonly": u.role == "auditor",
            "company": co.code if co else (v.name if v else "All companies"), "unread": unread or 0,
            "vendor": svc.vendor_out(v, True) if v else None}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/public")
def public(db=Depends(get_db)):
    """Tells the login page whether to show quick-login buttons. Credentials are only listed in demo mode."""
    out = {"demo_mode": DEMO_MODE, "users": []}
    if DEMO_MODE:
        for u in db.scalars(select(AppUser).order_by(AppUser.id)):
            co = db.get(Company, u.company_id).code if u.company_id else ""
            out["users"].append({"username": u.username, "name": u.name, "role": u.role, "company": co})
        out["password"] = seed.PASSWORD
    return out


@app.post("/api/login")
def login(body: LoginIn, db=Depends(get_db)):
    key = body.username.strip().lower()
    u = db.scalar(select(AppUser).where(AppUser.username == key))
    ok = security.check_pw(body.password, u.password_hash) if u else (security.burn_time(body.password) or False)
    if not ok:
        svc.log(db, None, "LOGIN_FAILED", f"Failed login for '{key[:40]}'")
        db.commit()
        raise HTTPException(401, "Wrong username or password")
    for old in db.scalars(select(UserSession).where(UserSession.user_id == u.id, UserSession.expires_at < now())):
        db.delete(old)
    token, h = security.new_token()
    t = now()
    db.add(UserSession(user_id=u.id, token_hash=h, created_at=t, last_seen=t, expires_at=t + dt.timedelta(hours=security.SESSION_HOURS)))
    svc.log(db, u, "LOGIN", "")
    db.commit()
    return {"token": token, "user": user_out(db, u)}


@app.post("/api/logout")
def logout(user=Depends(me), db=Depends(get_db)):
    s = db.scalar(select(UserSession).where(UserSession.token_hash == user._token_hash))
    if s:
        db.delete(s)
    svc.log(db, user, "LOGOUT", "")
    db.commit()
    return {"ok": True}


@app.post("/api/password")
def change_password(body: PasswordIn, user=Depends(me), db=Depends(get_db)):
    if not security.check_pw(body.current, user.password_hash):
        raise HTTPException(400, "Current password is wrong")
    if len(body.new) < security.MIN_PASSWORD:
        raise HTTPException(400, f"New password must be at least {security.MIN_PASSWORD} characters")
    user.password_hash = security.hash_pw(body.new)
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.token_hash != user._token_hash)):
        db.delete(s)  # every other device is logged out
    svc.log(db, user, "PASSWORD_CHANGE", "")
    db.commit()
    return {"ok": True}


@app.get("/api/me")
def get_me(user=Depends(me), db=Depends(get_db)):
    return user_out(db, user)


@app.get("/api/templates")
def templates(company_id: Optional[int] = None, user=Depends(me), db=Depends(get_db)):
    cid = user.company_id
    if not cid:  # admin and auditor pick a company; a vendor may ask only for companies whose POs it holds
        if user.role == "vendor" and not db.scalar(select(Document.id).where(Document.vendor_id == user.vendor_id, Document.company_id == company_id)):
            raise HTTPException(403, "No PO from that company")
        cid = company_id
    fields = {t: (svc.fields_for(db, cid, t) if cid else svc.TEMPLATES[t]) for t in svc.TEMPLATES}
    return {"fields": fields, "amount_label": svc.AMOUNT_LABEL}


@app.get("/api/dashboard")
def dashboard(company_id: Optional[int] = None, doc_type: Optional[str] = None, since: Optional[str] = None,
              until: Optional[str] = None, user=Depends(me), db=Depends(get_db)):
    return svc.dashboard(db, user, company_id, doc_type, to_utc(since) if since else None, to_utc(until) if until else None)


@app.get("/api/inbox")
def inbox(user=Depends(me), db=Depends(get_db)):
    return svc.inbox(db, user)


@app.get("/api/documents")
def list_docs(doc_type: Optional[str] = None, status: Optional[str] = None, q: Optional[str] = None,
              has_po: bool = False, stuck: bool = False, user=Depends(me), db=Depends(get_db)):
    out = []
    for d in db.scalars(svc.visible(db, user)):
        if doc_type and d.doc_type != doc_type or status and d.status not in status.split(","):
            continue
        b = svc.brief(db, d, user)
        if q and q.lower() not in f"{svc.ref(d)} {d.title} {b['vendor'] or ''} {d.data.get('invoice_no', '')}".lower():
            continue
        if has_po and not svc.sap_rec(db, d, "PR_PO"):
            continue
        if stuck and not b["stuck"]:
            continue
        out.append(b)
    return out


@app.post("/api/documents")
def create_doc(body: DocIn, user=Depends(writer), db=Depends(get_db)):
    d = svc.create_document(db, user, body.doc_type, body.title, body.amount, body.data, body.parent_id, body.vendor_id)
    return svc.detail(db, user, d)


@app.get("/api/documents/{doc_id}")
def get_doc(doc_id: int, user=Depends(me), db=Depends(get_db)):
    return svc.detail(db, user, doc_for(db, user, doc_id))


@app.patch("/api/documents/{doc_id}")
def patch_doc(doc_id: int, body: DocPatch, user=Depends(writer), db=Depends(get_db)):
    d = doc_for(db, user, doc_id)
    svc.update_document(db, user, d, body.title, body.amount, body.data)
    return svc.detail(db, user, d)


def flow(fn):
    def handler(doc_id: int, user=Depends(writer), db=Depends(get_db)):
        d = doc_for(db, user, doc_id)
        fn(db, user, d)
        return svc.detail(db, user, d)
    return handler


for path, fn in [("submit", svc.submit), ("resubmit", svc.resubmit), ("pay", svc.pay), ("nudge", svc.nudge)]:
    app.post(f"/api/documents/{{doc_id}}/{path}")(flow(fn))


class PostIn(BaseModel):
    park_user: str = ""
    ses_gr_no: str = ""


@app.post("/api/documents/{doc_id}/sap/post")
def sap_post(doc_id: int, body: PostIn, user=Depends(writer), db=Depends(get_db)):
    d = doc_for(db, user, doc_id)
    svc.post_invoice(db, user, d, body.park_user, body.ses_gr_no)
    return svc.detail(db, user, d)


@app.post("/api/documents/{doc_id}/action")
def action(doc_id: int, body: ActionIn, user=Depends(writer), db=Depends(get_db)):
    d = doc_for(db, user, doc_id)
    svc.act(db, user, d, body.decision, body.comment, body.received_value, body.ses_gr_no)
    return svc.detail(db, user, d)


@app.post("/api/documents/{doc_id}/comment")
def comment(doc_id: int, body: CommentIn, user=Depends(writer), db=Depends(get_db)):
    d = doc_for(db, user, doc_id)
    if not body.text.strip():
        raise HTTPException(400, "Write a comment first")
    db.add(Comment(document_id=d.id, user_id=user.id, text=body.text.strip()[:2000], created_at=now()))
    svc.log(db, user, "COMMENT", body.text.strip()[:200], d)
    db.commit()
    return svc.detail(db, user, d)


def check_upload(filename, raw):
    ext = Path(filename or "").suffix.lower()
    if ext not in SIGNATURES:
        raise HTTPException(400, "File type not allowed. Use " + ", ".join(sorted(SIGNATURES)))
    if not raw:
        raise HTTPException(400, "The file is empty")
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, f"File is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
    sig = SIGNATURES[ext]
    if sig is None:  # csv: must be text
        try:
            raw[:4096].decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "A .csv file must be plain text")
    elif not any(raw.startswith(s) for s in sig):
        raise HTTPException(400, f"This file does not look like a real {ext} file")
    return ext


@app.post("/api/documents/{doc_id}/attachment")
async def attach(doc_id: int, file: UploadFile = File(...), user=Depends(writer), db=Depends(get_db)):
    d = doc_for(db, user, doc_id)
    if "attach" not in svc.allowed(db, user, d):
        raise HTTPException(403, "You cannot attach files to this document now")
    if len(d.data.get("attachments", [])) >= MAX_FILES:
        raise HTTPException(400, f"At most {MAX_FILES} files per document")
    raw = await file.read(MAX_UPLOAD + 1)
    ext = check_upload(file.filename, raw)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file.filename).stem)[:60] or "file"
    import secrets
    safe = f"{d.id}_{secrets.token_hex(3)}_{base}{ext}"
    (UPLOADS / safe).write_bytes(raw)
    d.data = {**d.data, "attachments": [*d.data.get("attachments", []), safe]}
    svc.log(db, user, "ATTACH", safe, d)
    db.commit()
    return svc.detail(db, user, d)


@app.get("/api/documents/{doc_id}/attachments/{fname}")
def download(doc_id: int, fname: str, user=Depends(me), db=Depends(get_db)):
    d = doc_for(db, user, doc_id)
    if fname not in d.data.get("attachments", []):
        raise HTTPException(404, "File not found")
    return FileResponse(UPLOADS / fname, filename=fname.split("_", 2)[-1], content_disposition_type="attachment",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/notifications")
def notifications(user=Depends(me), db=Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(50))
    return [{"id": n.id, "to": n.to_address, "subject": n.subject, "body": n.body, "document_id": n.document_id,
             "at": n.created_at.isoformat(), "read": n.is_read} for n in rows]


@app.post("/api/notifications/read-all")
def read_all(user=Depends(me), db=Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.is_read == False)):  # noqa: E712
        n.is_read = True
    db.commit()
    return {"ok": True}


@app.post("/api/notifications/{nid}/read")
def read_one(nid: int, user=Depends(me), db=Depends(get_db)):
    n = db.get(Notification, nid)
    if n and n.user_id == user.id:
        n.is_read = True
        db.commit()
    return {"ok": True}


def pdf_response(db, user, docs, name, title=None):
    out = pdfgen.build([svc.detail(db, user, x) for x in docs], user.name, title)
    svc.log(db, user, "PDF_DOWNLOAD", name, docs[0])
    db.commit()
    return Response(out, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/documents/{doc_id}/pdf")
def doc_pdf(doc_id: int, user=Depends(me), db=Depends(get_db)):
    d = doc_for(db, user, doc_id)
    return pdf_response(db, user, [d], f"{svc.ref(d)}.pdf")


@app.get("/api/documents/{doc_id}/record.pdf")
def record_pdf(doc_id: int, user=Depends(me), db=Depends(get_db)):
    d = doc_for(db, user, doc_id)
    fam = [x for x in svc.family(db, d) if svc.can_see(user, x)]
    return pdf_response(db, user, fam, f"record-{svc.ref(fam[0])}.pdf", f"Purchase record: {fam[0].title}")


# ---------- audit ----------

def audit_filters(doc_id: Optional[int] = None, action: Optional[str] = None, role: Optional[str] = None,
                  username: Optional[str] = None, q: Optional[str] = None, since: Optional[str] = None,
                  until: Optional[str] = None, company_id: Optional[int] = None):
    return dict(doc_id=doc_id, action=action, role=role, username=username, q=q, since=since, until=until, company_id=company_id)


def to_utc(s):
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "Bad date filter")
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def audit_reader(user=Depends(me)):
    if user.role == "vendor":
        raise HTTPException(403, "Vendors cannot see the internal audit log")
    return user


def audit_query(user, f):
    q = select(AuditLog, AppUser.role).outerjoin(AppUser, AppUser.id == AuditLog.user_id).order_by(AuditLog.id.desc())
    if user.role not in svc.GLOBAL_ROLES:
        q = q.where(AuditLog.company_id == user.company_id)
    elif f["company_id"]:
        q = q.where(AuditLog.company_id == f["company_id"])
    if f["doc_id"]:
        q = q.where(AuditLog.document_id == f["doc_id"])
    if f["action"]:
        q = q.where(AuditLog.action.in_([x for x in f["action"].split(",") if x]))
    if f["role"]:
        q = q.where(AppUser.role == f["role"])
    if f["username"] and f["username"].strip():
        like = f"%{f['username'].strip()}%"
        q = q.where(or_(AuditLog.username.ilike(like), AppUser.name.ilike(like)))
    if f["q"] and f["q"].strip():
        q = q.where(AuditLog.detail.ilike(f"%{f['q'].strip()}%"))
    if f["since"]:
        q = q.where(AuditLog.at >= to_utc(f["since"]))
    if f["until"]:
        q = q.where(AuditLog.at <= to_utc(f["until"]))
    return q


@app.get("/api/audit")
def audit(limit: int = 100, offset: int = 0, f=Depends(audit_filters), user=Depends(audit_reader), db=Depends(get_db)):
    q = audit_query(user, f)
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery()))
    rows = db.execute(q.limit(min(max(limit, 1), 500)).offset(max(offset, 0))).all()
    companies = {c.id: c.code for c in db.scalars(select(Company))}
    scope = select(AuditLog.action).distinct()
    if user.role not in svc.GLOBAL_ROLES:
        scope = scope.where(AuditLog.company_id == user.company_id)
    return {"total": total, "actions": sorted(db.scalars(scope)),
            "rows": [{"id": a.id, "at": a.at.isoformat(), "user": a.username, "role": role or "", "company": companies.get(a.company_id, ""),
                      "action": a.action, "document_id": a.document_id, "detail": a.detail} for a, role in rows]}


@app.get("/api/audit/export")
def audit_export(f=Depends(audit_filters), user=Depends(audit_reader), db=Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "time_utc", "company", "user", "role", "action", "document_id", "detail"])
    companies = {c.id: c.code for c in db.scalars(select(Company))}
    for a, role in reversed(db.execute(audit_query(user, f)).all()):
        # a leading = + - @ would make Excel treat the cell as a formula
        cell = lambda s: ("'" + s) if s and s[0] in "=+-@" else s
        w.writerow([a.id, a.at.isoformat(sep=" ", timespec="seconds"), companies.get(a.company_id, ""), a.username, role or "",
                    a.action, a.document_id or "", cell(a.detail)])
    note = ", ".join(f"{k}={v}" for k, v in f.items() if v)
    svc.log(db, user, "AUDIT_EXPORT", "Audit log exported to CSV" + (f" ({note})" if note else ""))
    db.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=audit_log.csv"})


# ---------- configuration (admin) ----------

CHAIN_ROLES = {"PRPO": ("purchase",), "INVOICE": ("accounts", "approver"), "ION": ("approver",), "IOM": ("approver",), "TRN": ("approver",)}


def staff_only(user=Depends(me)):
    if user.role == "vendor":
        raise HTTPException(403, "Not available to vendors")
    return user


@app.get("/api/config")
def get_config(company_id: Optional[int] = None, user=Depends(staff_only), db=Depends(get_db)):
    cid = user.company_id or company_id
    if not cid:
        raise HTTPException(400, "Pick a company")
    rows = db.scalars(select(ApprovalConfig).where(ApprovalConfig.company_id == cid).order_by(ApprovalConfig.doc_type, ApprovalConfig.level))
    chains = {t: [] for t in svc.TEMPLATES}
    for r in rows:
        chains[r.doc_type].append({"level": r.level, "label": r.label, "kind": r.kind, "approver_id": r.approver_id,
                                   "approver": "The requester (PO raiser)" if r.kind == "requester" else svc.name(db, r.approver_id),
                                   "min_amount": r.min_amount or 0})
    people = [{"id": u.id, "name": u.name, "role": u.role} for u in db.scalars(
        select(AppUser).where(AppUser.company_id == cid, AppUser.role.in_(["approver", "purchase", "accounts"])).order_by(AppUser.id))]
    companies = [{"id": c.id, "code": c.code} for c in db.scalars(select(Company))]
    extras = {t: [{"label": f["label"], "ftype": f["type"], "required": not f.get("optional")} for f in svc.extras_for(db, cid, t)] for t in svc.TEMPLATES}
    return {"company_id": cid, "chains": chains, "people": people, "companies": companies, "extra_fields": extras,
            "roles_for": CHAIN_ROLES}


@app.put("/api/config")
def put_config(body: ConfigIn, user=Depends(me), db=Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(403, "Only the admin can change approval chains")
    if body.doc_type not in svc.TEMPLATES or not 1 <= len(body.levels) <= 6:
        raise HTTPException(400, "Use 1 to 6 approval levels")
    mins = [lv.min_amount for lv in body.levels]
    mins[0] = 0
    if any(a > b for a, b in zip(mins, mins[1:])):
        raise HTTPException(400, "Limits cannot decrease from one level to the next")
    people = [lv.approver_id for lv in body.levels if lv.kind != "requester"]
    if len(people) != len(set(people)):
        raise HTTPException(400, "The same person cannot hold two levels of one chain: nobody may approve two steps of a document, so it could never finish")
    for i, lv in enumerate(body.levels):
        if lv.kind == "requester":
            if body.doc_type != "INVOICE":
                raise HTTPException(400, "Only invoices can have a requester confirmation step")
        else:
            p = db.get(AppUser, lv.approver_id) if lv.approver_id else None
            if not p or p.company_id != body.company_id or p.role not in CHAIN_ROLES[body.doc_type]:
                raise HTTPException(400, f"Level {i + 1}: pick a {' or '.join(CHAIN_ROLES[body.doc_type])} from this company")
    for old in db.scalars(select(ApprovalConfig).where(ApprovalConfig.company_id == body.company_id, ApprovalConfig.doc_type == body.doc_type)):
        db.delete(old)
    for i, lv in enumerate(body.levels, 1):
        db.add(ApprovalConfig(company_id=body.company_id, doc_type=body.doc_type, level=i, label=(lv.label or "Approval").strip()[:60],
                              kind="requester" if lv.kind == "requester" else "fixed",
                              approver_id=None if lv.kind == "requester" else lv.approver_id, min_amount=mins[i - 1]))
    svc.log(db, user, "CONFIG", f"{body.doc_type} chain for company {body.company_id} set to {len(body.levels)} level(s)")
    db.commit()
    return get_config(body.company_id, user, db)


@app.put("/api/field-config")
def put_fields(body: FieldsIn, user=Depends(me), db=Depends(get_db)):
    if user.role != "admin":
        raise HTTPException(403, "Only the admin can change the extra fields")
    if body.doc_type not in svc.TEMPLATES or len(body.fields) > 15 or not db.get(Company, body.company_id):
        raise HTTPException(400, "Bad request, use at most 15 extra fields")
    core = {f["key"] for f in svc.TEMPLATES[body.doc_type]}
    keys, rows = set(), []
    for i, f in enumerate(body.fields):
        label = f.label.strip()[:60]
        if not label or f.ftype not in ("text", "number", "date", "textarea"):
            raise HTTPException(400, "Each field needs a label and a type of text, number, date or textarea")
        k, n = svc.slug(label), 2
        while k in keys or k in core:
            k = f"{svc.slug(label)}_{n}"
            n += 1
        keys.add(k)
        rows.append(FieldConfig(company_id=body.company_id, doc_type=body.doc_type, key=k, label=label, ftype=f.ftype, required=f.required, position=i))
    for old in db.scalars(select(FieldConfig).where(FieldConfig.company_id == body.company_id, FieldConfig.doc_type == body.doc_type)):
        db.delete(old)
    db.add_all(rows)
    svc.log(db, user, "FIELDS", f"{body.doc_type} extra fields for company {body.company_id}: " + (", ".join(r.label for r in rows) or "none"))
    db.commit()
    return get_config(body.company_id, user, db)


# ---------- vendors ----------

@app.get("/api/vendors")
def vendors(user=Depends(staff_only), db=Depends(get_db)):
    full = user.role in ("accounts", "admin", "auditor", "purchase")
    return [svc.vendor_out(v, full) for v in db.scalars(select(Vendor).order_by(Vendor.name))]


@app.post("/api/vendors")
def add_vendor(body: VendorIn, user=Depends(writer), db=Depends(get_db)):
    return svc.vendor_out(svc.create_vendor(db, user, body.name, body.gstin, body.email, body.einvoice_required, body.username, body.password), True)


@app.put("/api/vendors/me/bank")
def vendor_bank(body: BankIn, user=Depends(me), db=Depends(get_db)):
    return svc.vendor_out(svc.set_bank(db, user, body.account, body.ifsc), True)


@app.post("/api/vendors/{vid}/bank/verify")
def vendor_bank_verify(vid: int, body: VerifyIn, user=Depends(writer), db=Depends(get_db)):
    return svc.vendor_out(svc.verify_bank(db, user, vid, body.approve), True)


# ---------- delegation ----------

DELEGATING = ("approver", "purchase")


@app.get("/api/delegation")
def get_delegation(user=Depends(me), db=Depends(get_db)):
    if user.role not in DELEGATING:
        raise HTTPException(403, "Only approvers can delegate")
    mine = db.scalar(select(Delegation).where(Delegation.approver_id == user.id))
    others = db.scalars(select(AppUser).where(AppUser.company_id == user.company_id, AppUser.role == user.role, AppUser.id != user.id))
    covering = db.scalars(select(Delegation).where(Delegation.delegate_id == user.id))
    return {"delegate_id": mine.delegate_id if mine else None, "candidates": [{"id": u.id, "name": u.name} for u in others],
            "covering": [svc.name(db, c.approver_id) for c in covering]}


@app.put("/api/delegation")
def put_delegation(body: DelegateIn, user=Depends(me), db=Depends(get_db)):
    if user.role not in DELEGATING:
        raise HTTPException(403, "Only approvers can delegate")
    mine = db.scalar(select(Delegation).where(Delegation.approver_id == user.id))
    if body.delegate_id is None:
        if mine:
            db.delete(mine)
            svc.log(db, user, "DELEGATE_CLEAR", "Delegation ended")
    else:
        dg = db.get(AppUser, body.delegate_id)
        if not dg or dg.role != user.role or dg.company_id != user.company_id or dg.id == user.id:
            raise HTTPException(400, "Pick a colleague with the same role from your company")
        if mine:
            mine.delegate_id = dg.id
        else:
            db.add(Delegation(approver_id=user.id, delegate_id=dg.id, created_at=now()))
        svc.log(db, user, "DELEGATE", f"Delegated approvals to {dg.name}")
        svc.notify(db, dg.id, f"You are covering for {user.name}", f"{user.name} is away and delegated approvals to you. Their pending items appear in your inbox.")
    db.commit()
    return get_delegation(user, db)


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
