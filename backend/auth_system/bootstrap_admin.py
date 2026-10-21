"""Privileged local first-admin bootstrap; never expose this as an API route."""

import argparse
from datetime import datetime, timezone

from app import config as cfg
from auth_system.security import firebase_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--uid", default="")
    args = parser.parse_args()
    from firebase_admin import auth, firestore
    app = firebase_app()
    user = (auth.get_user(args.uid, app=app)
            if args.uid else auth.get_user_by_email(args.email, app=app))
    if (not user.email_verified or not user.email
            or user.email.strip().lower() not in cfg.ADMIN_EMAILS):
        raise SystemExit("Admin must have a verified email explicitly listed in ADMIN_EMAILS")
    now = datetime.now(timezone.utc)
    firestore.client(app=app, database_id=cfg.FIRESTORE_DATABASE_ID).collection("users").document(user.uid).set({
        "uid": user.uid, "email": user.email.lower(), "companyId": cfg.COMPANY_ID,
        "role": "admin", "status": "approved", "createdAt": now, "updatedAt": now,
        "bootstrappedBy": "local-command",
    }, merge=True)
    print(f"Bootstrapped approved admin profile for {user.email}")


if __name__ == "__main__":
    main()
