# Procure to Pay approval portal (demo)

FastAPI backend, SQLite (or Postgres/MySQL via `DATABASE_URL`), single-page frontend with no build step.
SAP and email are mocked: SAP numbers are generated on the server, email is stored and shown on the Mail page.

> A full README with screenshots is still to come. This file covers running, deploying and the rules the app enforces.

## Who does what

| Role | Does |
|---|---|
| Requester (`creator`) | Raises ION, IOM, TRN and the PR/PO request. Confirms work received on invoices of POs they raised. |
| Business approver (`approver`) | Approves ION, IOM, TRN and does finance approval on invoices. |
| Purchase team (`purchase`) | Approves PR/PO requests, levels by value. |
| Accounts (`accounts`) | First check on invoices, posts to SAP, releases payment, verifies vendor bank details. |
| Vendor (`vendor`) | Logs in, sees only POs addressed to them and their own invoices, submits invoices, answers queries, manages bank details. |
| Admin | Approval chains, extra fields per company, adds vendors. |
| Auditor | Read-only across all companies, including the audit log. |

## Run locally

    pip install -r requirements.txt
    cd backend
    uvicorn app:app --reload

Open http://127.0.0.1:8000. First start creates `p2p.db` and seeds demo data (delete `backend/p2p.db` to reset; an older database from a previous version is replaced automatically).
Run the tests with `cd backend && python test_flow.py`.

Demo logins (all passwords `demo1234`, shown as one-click buttons while `DEMO_MODE=1`): `<role>.<company>` such as `creator.hepl`, `approver1.hepl`, `purchase1.hepl`, `accounts.hepl`;
vendors `vendor.cooltech`, `vendor.betasec`, `vendor.alphatech`; plus `admin` and `auditor`. Companies: TUTPL, TRPL, UEPL, HEPL, DSEPL, DRL, PITCMRL, MRL.

## Deploy: Render (API) + Vercel (website)

1. **Push to GitHub.** Commit this folder as the repo root (it contains `render.yaml`, `vercel.json`, `requirements.txt`, `backend/`, `frontend/`).
2. **Render, the API.** New > Blueprint > pick the repo (it reads `render.yaml`), or New > Web Service with build `pip install -r requirements.txt` and start `cd backend && uvicorn app:app --host 0.0.0.0 --port $PORT`. Free plan. When it is live, open `https://<name>.onrender.com/api/health`, you should see `{"ok":true}`.
3. **Vercel, the website.** Edit `vercel.json` and replace `YOUR-RENDER-APP.onrender.com` with your Render host, commit and push. Then Add New > Project > import the repo. Framework preset: Other, no build command, output directory `frontend` (already set in `vercel.json`).
4. Open the Vercel URL. The site calls `/api/...` on its own address and Vercel forwards it to Render, so there is no CORS setup.

Things to know on free tiers:
- Render free services sleep after 15 minutes idle. The first request after that takes about a minute; open the health URL first before a demo.
- The free instance has no persistent disk, so SQLite data and uploaded files reset on every restart and redeploy. The app reseeds itself. For data that survives, create a Render Postgres and set `DATABASE_URL` on the web service (free Render Postgres expires after 30 days).
- Keep `MAX_UPLOAD_MB` small (4 on Render). Vercel's limit on proxied request size was not verified, so test an attachment upload through the Vercel URL.
- For anything beyond a demo set `DEMO_MODE=0` so the quick-login buttons and API docs are hidden, and change every password.

## What the app enforces

- **Chain:** ION, IOM, PR/PO, vendor invoice, payment. TRN is standalone with an optional IOM link. The PR/PO gets a PR number on submit and a PO number only after the purchase team approves.
- **Approval by amount** per company and document type; steps are copied on submit so config changes never alter documents in flight. Reject and query go back to the sender and, on resubmit, return to the same approver. Delegation between colleagues with the same role.
- **Invoices come from vendors**, only against an approved PO addressed to them. Queries go back to the vendor, who sees status and payment advice but never internal approver names or history.
- **Invoice chain by default:** Accounts check, then the requester who raised the PO confirms the work, then finance approval (a second finance level from INR 2.5 lakh). Editable per company, including the requester step.
- **GST:** amounts on PO and invoice are before GST; GST rate (0/5/12/18/28), HSN/SAC, GSTIN format check on vendors (format only, the checksum is not verified), IRN of 64 hex characters, mandatory when the vendor is flagged as under e-invoicing. Payment is on taxable value plus GST times the percent payable.
- **Per-company extra fields** on any document type (admin, "Chains and fields"); vendors see their company's extra invoice fields on the form.
- **Changed amounts restart approvals:** if a vendor changes the invoice amount after some approvals, all approvals (including the requester's work confirmation) start again. Other edits return to the same approver.
- **Chain sanity:** one person cannot hold two levels of the same chain, since nobody may approve two steps of a document.
- **Duplicate invoices:** the same invoice number from the same vendor is rejected. **Over-billing:** invoices on a PO cannot add up to more than the PO value.
- **Three-way match:** at the requester step they enter value received to date; invoiced to date must be within received plus `MATCH_TOLERANCE_PCT` (default 2%), otherwise approve is refused and a query is the way out.
- **Segregation of duties:** nobody approves a document they raised; nobody approves two steps of the same document; whoever raised the PO cannot post or pay its invoice.
- **Service entry / goods receipt:** the requester must enter the SES or GR number when confirming work; Accounts sees it before posting.
- **SAP park and post:** Accounts enters the park user (an internal person, never the vendor); the post user, date, day and time are recorded automatically.
- **Bank details:** a vendor's change must be verified by Accounts; payment is blocked until then.
- **Security:** bcrypt passwords, server-side sessions (12 h absolute, 4 h idle, several devices, logout, password change ends other sessions), uploads limited by size, extension and file signature (max 10 per document, forced download), security headers.
- **Audit log** is append-only at database level (SQLite triggers, tested; Postgres and MySQL triggers are written but untested), filterable and exportable to CSV.

## Configuration (environment variables)

See `.env.example`: `DATABASE_URL`, `DEMO_MODE`, `MAX_UPLOAD_MB`, `BCRYPT_ROUNDS`, `SESSION_HOURS`, `IDLE_HOURS`, `STUCK_DAYS`, `MATCH_TOLERANCE_PCT`.

## Demo walkthrough (5 minutes)

1. `vendor.cooltech`: dashboard shows POs from HEPL; open the pending invoice and see its review status; My bank details shows it is verified.
2. `accounts.hepl`: approve the Accounts check on the pending invoice. `creator.hepl`: enter value received and the SES/GR number, confirm and approve. `approver1.hepl` then finish finance approval. `accounts.hepl`: enter the SAP park user to record the SAP entry, then release payment.
3. `vendor.alphatech` proposed new bank details: `accounts.hepl` > Vendors > Verify.
4. Try to break it: duplicate invoice number, an invoice over the PO value, approve your own document, pay with unverified bank details.
5. `admin`: Chains and fields, add an extra invoice field for a company; then `vendor.cooltech` > New invoice shows it. `auditor`: read-only everywhere.
