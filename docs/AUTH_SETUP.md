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

The current frontend login gate is intentionally minimal. The full technician
profile form and responsive admin dashboard should be added after the company
domain and Firebase web-app values are supplied.
