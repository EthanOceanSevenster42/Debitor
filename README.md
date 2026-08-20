# FSA Debtor System — Deployment Guide

A Django web application for managing Xero debtors: collections (call / WhatsApp /
email follow-ups), handover and a legal (LBINC) workflow with the lawyers, recovery
tracking, role-based dashboards, a document filing archive, **outbound email**
(user invites, password resets, a weekly lawyer report and new-client alerts) and
self-service user onboarding.

This guide is for a backend developer deploying the app to a cloud server.

> ⚠️ **Read §6 (Email) and §7 (`SITE_BASE_URL`) carefully.** Every link the app
> emails to a user — password-reset links, invite links, and the "open the matter"
> links inside the lawyer report — is built from `SITE_BASE_URL`. If you leave it at
> the default it will point at `http://localhost:8000` and recipients' links will be
> broken. **Set it to the public server URL.**

---

## 1. Stack & architecture

| Part | Detail |
|------|--------|
| Language / framework | Python 3.14, **Django 6.0** |
| Database | **SQL Server** (default) via `mssql-django` + `pyodbc` (OS needs **ODBC Driver 18 for SQL Server**), **or PostgreSQL** — selected by `DB_ENGINE` (`mssql` / `postgresql`). DB name `FSA_Debtors`. |
| External API (in) | **Xero** (OAuth 2.0; read-only accounting scopes) — invoice data |
| External API (out) | **Microsoft Graph** (`sendMail`, app-only) — all outbound email |
| Background jobs | Hourly **`manage.py sync_xero`** (Xero → SQL); hourly **`manage.py send_lawyer_report`** (self-gated weekly report) |
| PDF | `reportlab` — the weekly lawyer report PDF |
| Uploads | User-attached documents stored under `MEDIA_ROOT` (needs persistent storage) |
| Frontend | Server-rendered Django templates (no separate JS build) |

It is a **single Django process** (it serves both the UI and the API). There is no
separate frontend to deploy.

---

## 2. Prerequisites on the server

1. **Python 3.14** (3.12+ should also work).
2. **Microsoft ODBC Driver 18 for SQL Server** installed on the OS:
   - Debian/Ubuntu: follow Microsoft's `msodbcsql18` apt instructions.
   - Windows: install "ODBC Driver 18 for SQL Server".
3. **A SQL Server database** the app can reach — Azure SQL, SQL Server on a VM, or
   AWS RDS for SQL Server. Create an empty database named `FSA_Debtors` (or set
   `DB_NAME`) and a login with `db_owner` on it.
4. **A Xero app** (https://developer.xero.com) with the production **redirect URI**
   registered (see §5).
5. **A Microsoft Entra (Azure AD) app registration** with `Mail.Send` application
   permission, and a sending mailbox (see §6).
6. A reverse proxy that terminates **HTTPS** (nginx, Caddy, or the platform's load
   balancer). Xero OAuth and secure cookies require HTTPS in production.

---

## 3. Environment variables

The app reads configuration from environment variables (or a `.env` file next to
`manage.py` — `python-dotenv` loads it). Copy **`.env.example` → `.env`** and fill in:

### Core
| Variable | Required | Notes |
|----------|----------|-------|
| `XERO_CLIENT_ID` / `XERO_CLIENT_SECRET` | yes | From the Xero app |
| `XERO_REDIRECT_URI` | yes | Must EXACTLY match the Xero app's redirect URI, e.g. `https://YOUR-DOMAIN/xero/callback/` |
| `DJANGO_SECRET_KEY` | yes | Long random string — generate a fresh one |
| `DJANGO_DEBUG` | yes | **`False`** in production |
| `DJANGO_ALLOWED_HOSTS` | yes | Comma-separated, e.g. `debtors.example.com` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | yes | e.g. `https://debtors.example.com` |
| `DB_ENGINE` | optional | `mssql` (default) or `postgresql`. For Postgres, `pip install "psycopg[binary]"`. |
| `DB_SERVER` | yes | DB host |
| `DB_NAME` | yes | `FSA_Debtors` |
| `DB_PORT` | usually | `1433` (mssql) / `5432` (postgresql) |
| `DB_USER` / `DB_PASSWORD` | yes (cloud) | DB login. **Leave blank only** for local Windows (mssql) trusted auth. |
| `DB_DRIVER` | optional (mssql) | Defaults to `ODBC Driver 18 for SQL Server` |

### Email + links (NEW — see §6, §7)
| Variable | Required | Notes |
|----------|----------|-------|
| `MS_GRAPH_TENANT_ID` | for email | Entra **Directory (tenant) ID** |
| `MS_GRAPH_CLIENT_ID` | for email | The app registration's **Application (client) ID** |
| `MS_GRAPH_CLIENT_SECRET` | for email | A client **secret value** (rotate before go-live if it was ever shared) |
| `MS_GRAPH_SENDER` | for email | Mailbox to send **as**, e.g. `accounts@yourdomain` (a shared mailbox is fine — no licence needed for Graph app-only send) |
| `DEFAULT_FROM_EMAIL` | optional | Friendly From label; defaults to `MS_GRAPH_SENDER` |
| **`SITE_BASE_URL`** | **yes for links** | **Public URL of the app**, e.g. `https://debtors.example.com`. Used to build every link inside outbound emails/PDFs. **Defaults to `http://localhost:8000` — you MUST change it.** |
| `REPORT_RECIPIENTS` | optional | Comma-separated fallback recipients for the lawyer report |
| `PASSWORD_RESET_TIMEOUT` | optional | Seconds a reset/invite link stays valid (default 3 days) |

> When `DB_USER` is set the app uses **SQL authentication** (the cloud case). When
> it's blank it falls back to **Windows trusted auth** (local dev only).
>
> If the `MS_GRAPH_*` values are left blank, email falls back to a **console backend**
> (messages are logged, not sent) so the app still runs — but invites, password
> resets, and the lawyer report won't actually deliver until email is configured.

---

## 4. Install & first-time setup

```bash
# 1. Get the code onto the server (this Debitor-main folder), then:
cd Debitor-main
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # includes reportlab (PDF) + Graph uses requests

# 2. Create .env from the template and fill it in (Xero, DB, Graph, SITE_BASE_URL)
cp .env.example .env                # then edit .env

# 3. Sanity check + create the schema
python manage.py check
python manage.py migrate            # creates all tables in FSA_Debtors

# 4. Collect static files (served by nginx / the platform)
python manage.py collectstatic --noinput   # outputs to ./staticfiles

# 5. Create the first Super Admin login
python manage.py createsuperuser    # email + password (role = Super Admin)
```

---

## 5. Configure the Xero app

In the Xero developer portal, on the app whose `XERO_CLIENT_ID` you're using:

- Add the **redirect URI**: `https://YOUR-DOMAIN/xero/callback/` (must match
  `XERO_REDIRECT_URI` exactly, including the trailing slash).
- Scopes used (read-only): `openid profile email offline_access
  accounting.contacts.read accounting.invoices.read accounting.settings.read`.

After deployment, a Super Admin logs in → **Connect to Xero** → authorises → the app
stores the tenant token and can sync.

---

## 6. Configure email (Microsoft Graph)

All outbound mail — **user invites, password resets, the weekly lawyer report and
new-client alerts** — is sent through the **Microsoft Graph API** using an Entra app
registration (client-credentials / app-only). **No SMTP, and the sending mailbox does
not need a licence** for app-only Graph send.

One-time setup in the Microsoft **Entra admin center**:

1. **App registrations → New registration** (single tenant). Note the
   **Application (client) ID** and **Directory (tenant) ID**.
2. **Certificates & secrets → New client secret** → copy the **Value** (not the ID).
3. **API permissions → Add a permission → Microsoft Graph → Application permissions →
   `Mail.Send`** → then **Grant admin consent**. (This is the step that's easy to
   miss; without consent every send returns `403`.)
4. Choose the sending mailbox (a **shared mailbox** such as `accounts@yourdomain` is
   ideal). Optionally scope the app to just that mailbox with an Exchange
   **Application Access Policy**.
5. Put the four values in `.env`: `MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`,
   `MS_GRAPH_CLIENT_SECRET`, `MS_GRAPH_SENDER`.

**Verify it works** (sends one real test message):
```bash
python manage.py send_test_email you@yourdomain.com
```
It prints the active backend + sender and reports success/failure. A `403` almost
always means step 3 (admin consent for `Mail.Send`) is missing.

> 🔒 Treat `MS_GRAPH_CLIENT_SECRET` like a password: keep it only in `.env`
> (git-ignored), and **rotate** it if it has ever been shared in plaintext.

---

## 7. Public links — `SITE_BASE_URL` (do not skip)

Outbound email has no incoming web request to derive the site address from, so the
app builds links from **`SITE_BASE_URL`**. This affects:

- **Password-reset** links (login page → "Forgot your password?").
- **User invite** links (Users → Invite User → the new user sets their password).
- The **"Open the matter"** links inside the **lawyer report PDF** and the
  **new-client alert** email.

`SITE_BASE_URL` **defaults to `http://localhost:8000`**. If you deploy without
changing it, every emailed link points at the developer's local machine and will not
work for the recipient.

**Set it to the real, public, HTTPS address of the server**, with no trailing slash:
```ini
SITE_BASE_URL=https://debtors.example.com
```
After changing it, send a `send_test_email` and (optionally) a test lawyer report,
open the email, and click a link to confirm it lands on the live site.

---

## 8. Running the app (production)

**Do not** use `manage.py runserver` in production. Use a WSGI server behind HTTPS.

### Linux (gunicorn + nginx) — recommended
```bash
gunicorn mysite.wsgi:application --bind 127.0.0.1:8000 --workers 3 --timeout 120
```
Put it behind nginx (or the platform LB) terminating TLS and forwarding to
`127.0.0.1:8000`. nginx should also serve the static & media files:

```nginx
location /static/ { alias /path/to/Debitor-main/staticfiles/; }
location /media/  { alias /path/to/Debitor-main/media/; }
location /        { proxy_pass http://127.0.0.1:8000;
                    proxy_set_header Host $host;
                    proxy_set_header X-Forwarded-Proto $scheme; }
```
Run gunicorn as a **systemd** service so it restarts on boot/crash.

### Windows (IIS)
Use **IIS + wfastcgi** (or run gunicorn's Windows equivalent `waitress`). IIS serves
`\staticfiles` and `\media` as virtual directories.

> The app already trusts `X-Forwarded-Proto` and sets secure cookies when
> `DJANGO_DEBUG=False`, so it works correctly behind an HTTPS proxy.

---

## 9. Scheduled jobs

Two commands must run on a schedule. Both **self-gate** — they decide internally
whether to do anything — so it's safe (and intended) to fire them **hourly**.

| Command | What it does | Cadence controlled by |
|---------|--------------|------------------------|
| `manage.py sync_xero` | Pulls open invoices Xero → SQL (rate-limited to half of Xero's limits) | In-app **Schedule** page |
| `manage.py send_lawyer_report` | Sends the weekly lawyer report **if due** | In-app **Lawyer Report** page |

### Linux — systemd timers (recommended)

The repo ships the units plus an idempotent installer, so the schedule is
version-controlled rather than hand-typed on the box:

```bash
sudo deploy/install-timers.sh              # install / refresh, then enable
sudo deploy/install-timers.sh --status     # what's scheduled and when it next runs
sudo deploy/install-timers.sh --uninstall  # stop, disable, remove
journalctl -u fsa-sync -u fsa-lawyer-report -n 50
```

It infers the checkout path, `.venv/bin/python` and the user owning `manage.py`;
override with `APP_DIR=`, `PYTHON=`, `RUN_AS=`. Re-running rewrites the units from
`deploy/systemd/`, so **append it to the server's `deploy-dms` script** and every
deploy re-asserts the schedule from the repo.

Both timers are `Persistent=true`: a slot missed because the server was down runs
on the way back up, so a week's report isn't silently skipped.

### Linux — cron (if systemd isn't available)
```cron
# every hour, on the hour
0 * * * * cd /path/to/Debitor-main && /path/to/.venv/bin/python manage.py sync_xero >> /var/log/fsa_sync.log 2>&1
5 * * * * cd /path/to/Debitor-main && /path/to/.venv/bin/python manage.py send_lawyer_report >> /var/log/fsa_report.log 2>&1
```

### Windows — Task Scheduler
The repo ships **`run_sync.bat`**, which now runs **both** `sync_xero` **and**
`send_lawyer_report`. Register it as an hourly Scheduled Task. Edit the Python path
inside the .bat to match the server.

> The report's actual day/time/frequency and recipient list are configured in-app
> under **Lawyer Report** (Super Admin). The OS job just needs to fire at least
> hourly; the app decides whether to send. Sending is **off by default** until a
> Super Admin enables it and adds recipients.

---

## 10. Files / persistent storage

- **Uploads** (`media/`) — invoice-comment and legal-document attachments live here.
  On ephemeral cloud filesystems (containers), mount a **persistent volume** at
  `media/`, or switch to object storage (S3/Azure Blob via `django-storages`).
  These documents are surfaced in the **Filing** archive, so losing `media/` loses
  the filing.
- **Static** (`staticfiles/`) — produced by `collectstatic`; served by nginx/IIS.

---

## 11. Go-live checklist

- [ ] `DJANGO_DEBUG=False`
- [ ] `DJANGO_SECRET_KEY` is a fresh long random value (not the dev default)
- [ ] `DJANGO_ALLOWED_HOSTS` + `DJANGO_CSRF_TRUSTED_ORIGINS` set to the real domain
- [ ] **`SITE_BASE_URL` set to the public `https://` URL** (not localhost) ← links
- [ ] HTTPS enforced at the proxy; HTTP redirects to HTTPS
- [ ] `XERO_REDIRECT_URI` matches the Xero app and uses `https://`
- [ ] `MS_GRAPH_*` set; `Mail.Send` admin-consented; `send_test_email` delivers
- [ ] `MS_GRAPH_CLIENT_SECRET` rotated if it was ever shared in plaintext
- [ ] `migrate` run; `createsuperuser` done; `collectstatic` done
- [ ] Hourly `sync_xero` **and** `send_lawyer_report` scheduled (`sudo deploy/install-timers.sh`)
- [ ] `media/` on persistent storage; database **backups** enabled
- [ ] `python manage.py check --deploy` reviewed

---

## 12. First run after deploy

1. Browse to `https://YOUR-DOMAIN/` and log in as the Super Admin.
2. **Connect to Xero** and authorise.
3. Trigger the first sync: **Schedule → Refresh now** (or wait for the hourly job).
4. Send `python manage.py send_test_email you@yourdomain.com` and confirm delivery.
5. Create the other users under **Users** — use **Invite User** so they set their own
   password via the emailed link (confirm the link points at the live domain).
6. Configure the **Lawyer Report** (recipients, day/time) and enable it.
7. Optionally set the **go-live date** (Schedule) and review **Communication Setup**.

Then run the **UAT** (`UAT.md` / `UAT.docx`) to verify everything end-to-end.

---

## 13. Troubleshooting

| Symptom | Likely cause |
|--------|--------------|
| `Invalid HTTP_HOST header` | Domain missing from `DJANGO_ALLOWED_HOSTS` |
| CSRF "Origin checking failed" on a form POST | Domain missing from `DJANGO_CSRF_TRUSTED_ORIGINS`, or not on HTTPS |
| Xero "redirect_uri mismatch" | `XERO_REDIRECT_URI` ≠ the URI registered on the Xero app |
| `Can't open lib 'ODBC Driver 18...'` | ODBC Driver 18 not installed on the OS |
| Login works but no data | Xero not connected, or `sync_xero` hasn't run yet |
| Emails not sending | `MS_GRAPH_*` blank (console backend), or `Mail.Send` not admin-consented (`403`) |
| Emailed links point at `localhost` | `SITE_BASE_URL` not set to the public URL (see §7) |
| Lawyer report never arrives | Report disabled, no recipients, or `send_lawyer_report` not scheduled |
| Uploaded documents disappear after redeploy | `media/` not on persistent storage |

---

## 14. Roles & access levels

Every login has one **role** (set under **Users**). Access is enforced in the app
(not just hidden in the menu). There are **three roles** (the old *Inspector* role has
been removed).

| Role | Can access / do | Cannot do |
|------|-----------------|-----------|
| **Super Admin** | **Everything** — Dashboard (system-wide), Debtors Action, Closed Debtors, Write-offs, Handover, Lawyers, **Filing**, Users, Schedule, **Lawyer Report**, Communication Setup. Manage users; allocate debtors; close/reopen; write off; handover rules + marking; **send to lawyers, approve, bring back**; plus all collections actions. | — |
| **Administrator** | **Collections only.** Dashboard (their own book), Debtors Action (their allocated debtors): log **calls / WhatsApps / emails**, add comments + **upload documents** (with a date + nature), set a debtor's **follow-up cadence shift**. May **view** the Lawyers page (read-only). | Close/reopen, write off, handover, allocate, send-to / approve lawyers, and the Filing / Users / Schedule / Lawyer Report / Communication Setup pages. |
| **Lawyer** | **Lawyers page + Filing.** Their approved (active) legal matters: tick steps, comment + **upload documents** (with a date + nature), switch a route Unopposed ⇄ Opposed; browse the **Filing** archive. | Debtor, dashboard and handover pages (they redirect to the Lawyers page). |

A new user created without an explicit role has **no access** until a Super Admin
assigns one. The first account (`createsuperuser`) is a **Super Admin**.

**Approval rule:** an Administrator (or Super Admin) **sends** a company to the
lawyers, but **only a Super Admin can approve** it before it appears on the Lawyers
page — and only a Super Admin can **bring it back**. On approval, the configured
**Lawyer Report recipients** receive a "new client needs attention" alert.

**Dashboards** are role-specific:
- **Super Admin** — system-wide charts (outstanding by month, age donut, recovered-vs-outstanding bars), per-admin tracking, critical debtors and action items.
- **Administrator** — the same, **scoped to their own allocated book**.
- **Lawyer** — the Lawyers page with matter-progress KPIs.

---

## 15. What changed recently (for the deploying dev)

Since the previous build:

- **Outbound email via Microsoft Graph** — new `MS_GRAPH_*` env vars and a custom
  Django email backend (`xero_app/mail_backend.py`). All Django mail flows through it.
- **`SITE_BASE_URL`** — new; required for correct links in emails/PDFs (see §7).
- **User invites & self-service password reset** — invite links and the login-page
  "Forgot your password?" flow (both emailed).
- **Weekly lawyer report** — a scheduled, configurable PDF (KPIs, severity colours,
  links). New dependency **`reportlab`**; new command `send_lawyer_report`;
  `run_sync.bat` now also runs it.
- **New-client approval alert** — emails the lawyers when a matter is approved.
- **Filing** — now shows a document count per company, is open to **Lawyers** (not
  just Super Admins), and each uploaded file records its **date + nature**.
- **Inspector role removed** — only Super Admin / Administrator / Lawyer remain.
- **New management commands:** `send_test_email`, `send_lawyer_report`.
- Run `python manage.py migrate` on deploy to apply the new schema (attachment
  `nature`, report config/recipients, the role change).
