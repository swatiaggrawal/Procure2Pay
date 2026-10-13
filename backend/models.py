import datetime as dt
import os
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# SQLite by default. For Postgres (Render) set DATABASE_URL; the common postgres:// form is fixed up here.
DB_URL = os.getenv("DATABASE_URL", "sqlite:///./p2p.db")
if DB_URL.startswith("postgres://"):
    DB_URL = "postgresql+psycopg2://" + DB_URL[len("postgres://"):]
elif DB_URL.startswith("postgresql://"):
    DB_URL = "postgresql+psycopg2://" + DB_URL[len("postgresql://"):]
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "company"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(100))


class Vendor(Base):
    """External supplier. One vendor can serve several companies."""
    __tablename__ = "vendor"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    gstin: Mapped[str] = mapped_column(String(15))
    email: Mapped[str] = mapped_column(String(120))
    einvoice_required: Mapped[bool] = mapped_column(Boolean, default=False)  # above the e-invoicing threshold, so IRN is mandatory
    bank_account: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    ifsc: Mapped[Optional[str]] = mapped_column(String(11), nullable=True)
    bank_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_bank_account: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pending_ifsc: Mapped[Optional[str]] = mapped_column(String(11), nullable=True)
    pending_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[Optional[int]] = mapped_column(ForeignKey("company.id"), nullable=True)
    vendor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vendor.id"), nullable=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(120))
    # creator (requester) | approver | purchase | accounts | vendor | admin | auditor (read only)
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(80))


class UserSession(Base):
    """Login sessions. Only a hash of the token is stored; a user can have several sessions."""
    __tablename__ = "user_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime)


class ApprovalConfig(Base):
    __tablename__ = "approval_config"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
    doc_type: Mapped[str] = mapped_column(String(10))
    level: Mapped[int] = mapped_column()
    label: Mapped[str] = mapped_column(String(60), default="Approval")
    kind: Mapped[str] = mapped_column(String(10), default="fixed")  # fixed person, or requester (whoever raised the PO)
    approver_id: Mapped[Optional[int]] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    min_amount: Mapped[float] = mapped_column(Float, default=0)  # level applies when the document amount is at least this


class FieldConfig(Base):
    """Extra fields a company wants on a document type, on top of the shared core fields."""
    __tablename__ = "field_config"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
    doc_type: Mapped[str] = mapped_column(String(10))
    key: Mapped[str] = mapped_column(String(50))
    label: Mapped[str] = mapped_column(String(60))
    ftype: Mapped[str] = mapped_column(String(10), default="text")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(default=0)


class Document(Base):
    __tablename__ = "document"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
    doc_type: Mapped[str] = mapped_column(String(10))  # ION | IOM | TRN | PRPO | INVOICE
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("document.id"), nullable=True)
    vendor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vendor.id"), nullable=True)  # set on PRPO and INVOICE
    created_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12))  # DRAFT PENDING QUERY REJECTED APPROVED PAID
    current_level: Mapped[int] = mapped_column(default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)  # always before GST
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ApprovalStep(Base):
    __tablename__ = "approval_step"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    level: Mapped[int] = mapped_column()
    label: Mapped[str] = mapped_column(String(60), default="Approval")
    kind: Mapped[str] = mapped_column(String(10), default="fixed")
    approver_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    decision: Mapped[str] = mapped_column(String(10), default="pending")  # pending approved rejected query
    comment: Mapped[str] = mapped_column(Text, default="")
    acted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    acted_by: Mapped[Optional[int]] = mapped_column(nullable=True)  # differs from approver_id when a delegate acted


class Delegation(Base):
    """While set, the delegate can act on the approver's pending steps."""
    __tablename__ = "delegation"
    id: Mapped[int] = mapped_column(primary_key=True)
    approver_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), unique=True)
    delegate_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Comment(Base):
    __tablename__ = "comment"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class SapRecord(Base):
    """What SAP hands back. kind=PR_PO on a PRPO, kind=INVOICE_POST on an invoice."""
    __tablename__ = "sap_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    kind: Mapped[str] = mapped_column(String(20))
    pr_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    po_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    sap_doc_no: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_by_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    approved_by_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    park_user: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    post_user: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Payment(Base):
    __tablename__ = "payment"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    amount: Mapped[float] = mapped_column(Float)
    reference: Mapped[str] = mapped_column(String(30))
    paid_by: Mapped[str] = mapped_column(String(100))
    paid_at: Mapped[dt.datetime] = mapped_column(DateTime)


class AuditLog(Base):
    """Append only. The database itself refuses UPDATE and DELETE, see install_audit_guards."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    username: Mapped[str] = mapped_column(String(60))
    company_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    document_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime)


class Notification(Base):
    """Mock email. Stored instead of sent, so the demo needs no SMTP."""
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    to_address: Mapped[str] = mapped_column(String(120))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    document_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)


def install_audit_guards(eng=None):
    """Make audit_log append only at database level, so history cannot be quietly edited through the application's
    connection. SQLite is tested; the Postgres and MySQL statements are best effort."""
    eng = eng or engine
    d = eng.dialect.name
    if d == "sqlite":
        stmts = [
            "CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
        ]
    elif d == "postgresql":
        stmts = [
            "CREATE OR REPLACE FUNCTION audit_log_guard() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$ LANGUAGE plpgsql",
            "DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log",
            "CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_guard()",
        ]
    elif d in ("mysql", "mariadb"):
        stmts = [
            "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_log is append-only'",
            "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_log is append-only'",
        ]
    else:
        return
    for s in stmts:
        try:
            with eng.begin() as cx:
                cx.execute(text(s))
        except Exception as e:  # already exists, or the DB user may not create triggers
            print(f"audit guard skipped: {str(e)[:120]}")
