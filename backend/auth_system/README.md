# Authentication system (isolated)

This folder contains the Firebase Identity Platform and Firestore approval
implementation plan and templates. It is intentionally not imported by the
existing chatbot application, so the current application behavior is unchanged.

Before wiring it into `app/main.py`, install `firebase-admin`, configure the
placeholders in `config.example`, and complete the Firebase/Firestore setup in
the project documentation.

The intended flow is: Firebase email/password sign-up -> verified email ->
FastAPI registration completion -> pending Firestore profile -> admin decision
-> approved technician access.
