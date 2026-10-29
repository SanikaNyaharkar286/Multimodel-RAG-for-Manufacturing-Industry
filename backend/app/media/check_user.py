# small script to look up a firebase user by email.
# it prints the user's id, and whether the email is verified and the account is disabled.
import firebase_admin
from firebase_admin import auth

PROJECT_ID = "market-lens-506611"

# connect to firebase, but only once
if not firebase_admin._apps:
    firebase_admin.initialize_app(
        options={"projectId": PROJECT_ID}
    )

# find the user by email
email = "sanikanyaharkar@gmail.com"
user = auth.get_user_by_email(email)

# show the user's details
print("UID:", user.uid)
print("Email verified:", user.email_verified)
print("Disabled:", user.disabled)