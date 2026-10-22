import firebase_admin
from firebase_admin import auth

PROJECT_ID = "market-lens-506611"

if not firebase_admin._apps:
    firebase_admin.initialize_app(
        options={"projectId": PROJECT_ID}
    )

email = "sanikanyaharkar@gmail.com"
user = auth.get_user_by_email(email)

print("UID:", user.uid)
print("Email verified:", user.email_verified)
print("Disabled:", user.disabled)