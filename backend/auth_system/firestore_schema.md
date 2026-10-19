# Firestore schema

- `users/{uid}`: server-owned profile, role, status, company and review fields.
- `accessRequests/{requestId}`: immutable registration request and current decision.
- `adminNotifications/{notificationId}`: pending requests and unread state.
- `userNotifications/{notificationId}`: approval or rejection messages.
- `auditLogs/{eventId}`: acting admin, previous status, new status, reason and server timestamp.

Allowed statuses are `pending`, `approved`, `rejected`, and `suspended`. A
browser never supplies role, status, reviewer, company ID, or another user's UID.
