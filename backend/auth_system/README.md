# Authentication and administrator approval

This package is connected to FastAPI and the frontend authentication gate.
The flow is Firebase sign-up -> email verification -> technician profile ->
pending access request -> administrator review -> approved technician access.

After a new pending profile is saved, `app/services/email_service.py` attempts
an SMTP notification to `ADMIN_NOTIFICATION_EMAIL`. Registration runs in a
worker thread, so Firestore and SMTP do not block the FastAPI event loop.
Missing settings and email failures are logged without failing registration.
Failed deliveries are recorded in the existing `adminNotifications` document
and retried while the technician remains pending. Existing profile retries and
admin decisions do not create notifications.
See `docs/AUTH_SETUP.md` for settings, logs and the manual test plan.
