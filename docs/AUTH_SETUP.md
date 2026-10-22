# Authentication setup

The repository now contains Firebase Authentication and Firestore approval
code. Cloud-console steps remain manual because they require your Firebase
account and the company's real email domain.

## Local files

From `backend`, copy the auth values into `.env`:

```env
AUTH_ENABLED=true
FIREBASE_PROJECT=market-lens-506611
COMPANY_ID=COMPANY_ID
COMPANY_NAME=COMPANY_NAME
ALLOWED_EMAIL_DOMAINS=your-company.com
```

From `frontend`, create `.env` with the Firebase web-app values:

```env
VITE_API_URL=http://127.0.0.1:8000
VITE_AUTH_ENABLED=true
VITE_FIREBASE_API_KEY=your-web-api-key
VITE_FIREBASE_AUTH_DOMAIN=market-lens-506611.firebaseapp.com
VITE_FIREBASE_PROJECT_ID=market-lens-506611
```

## Firebase console

1. Open Firebase Console and select `market-lens-506611`.
2. Add a Web app under Project settings and copy its config values into
   `frontend/.env`.
3. Open Authentication, Sign-in method, enable Email/Password, and save.
4. Add `localhost` and the production frontend hostname under Authorized domains.
5. Open Firestore Database and create the database in the region chosen by the
   company. Do not use a production collection until rules and IAM are reviewed.

## First admin

Create the admin user in Firebase Authentication, verify its email, then run
from `backend` with Application Default Credentials:

```powershell
python -m auth_system.bootstrap_admin --email admin@your-company.com
```

## Run locally

```powershell
# backend terminal
cd E:\manufacturing_agent\Multimodel-RAG-for-Manufacturing-Industry\backend
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload --port 8000

# frontend terminal
cd E:\manufacturing_agent\Multimodel-RAG-for-Manufacturing-Industry\frontend
npm.cmd run dev
```

The frontend includes the technician profile form, pending/rejected/suspended
screens and an administrator approval portal. Pending technicians automatically
refresh approval status every ten seconds. The administrator list refreshes
every thirty seconds and also has a manual refresh button.

## Administrator email notifications

After a brand-new pending profile is committed, the backend sends an email to
`ADMIN_NOTIFICATION_EMAIL`. The recipient setting does not grant admin
privileges. Existing profile retries, logins and admin decisions do not create
new notifications.

Add these values to `backend/.env` and restart the backend:

```dotenv
ADMIN_NOTIFICATION_EMAIL=admin@your-company.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-sender-email@gmail.com
SMTP_PASSWORD=your-gmail-app-password
SMTP_FROM_EMAIL=your-sender-email@gmail.com
SMTP_FROM_NAME=Manufacturing Equipment Copilot
SMTP_USE_TLS=true
APPROVAL_PORTAL_URL=https://your-frontend-hostname
```

`SMTP_USE_TLS=true` uses STARTTLS with certificate verification. Keep passwords
in the local environment or deployment secret store. The existing config loader
reads `backend/.env`; deployment environment values take precedence. Missing or
invalid mail settings log a warning and skip email without preventing startup
or registration. The old plural recipient and enable-flag settings are unused.

The portal URL must be the frontend address reachable from the administrator's
device. A localhost link works only on the machine hosting that frontend.
Emails include the technician's name, email, company, employee ID, phone,
department, site, designation, reason, UTC request time and a review link.
The subject is `New Technician Approval Request`. Opening it shows the selected
request after administrator login; the email itself never approves an account.

The registration endpoint runs the blocking Firestore and SMTP work through
`asyncio.to_thread`. Email runs only after the profile and existing in-app
notification commit successfully. SMTP errors never undo the pending profile
or appear in API responses. The API retains the existing profile fields and adds
`message: Approval request submitted successfully.`

Logs contain `Pending approval request created`, `Admin email notification sent
successfully` or `Admin email notification failed`. Failure logs include only
the user ID, error category and numeric SMTP code, not provider replies or secrets.
The matching `adminNotifications` document records `emailStatus`, attempt times
and the successful send time. Failed notifications are retried once per minute,
up to ten attempts, while the technician remains pending. Approval, rejection
or suspension stops retries. SMTP acceptance does not prove inbox delivery.

### Automated tests

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_approval_email -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Tests mock Firestore and SMTP and send no real email.

### Manual test plan

1. Add SMTP and admin email values to backend/.env.
2. Start the backend.
3. Register a brand-new technician account.
4. Confirm a pending profile is created in Firestore.
5. Confirm the admin receives the notification email.
6. Check backend logs for “Admin email notification sent”.
7. Temporarily use invalid SMTP credentials and restart the backend.
8. Register another new technician.
9. Confirm the pending profile is still created and backend logs show an email notification failure.
10. Restore valid SMTP credentials and restart. Leave the request pending and
    confirm the background retry sends it without resubmitting registration.
    Then approve/reject it and confirm the decision sends no extra email.
11. Temporarily remove the admin recipient or SMTP host and restart; a new
    registration must still succeed with a warning that notification was skipped.
