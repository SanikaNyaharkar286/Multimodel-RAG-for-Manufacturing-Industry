import { APP_CONFIG } from "../config/appConfig";

async function authenticatedRequest(user, path, options = {}) {
  const token = await user.getIdToken();
  const response = await fetch(`${APP_CONFIG.apiBaseUrl}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
      Authorization: `Bearer ${token}`,
    },
  });
  let result = {};
  try { result = await response.json(); } catch { /* A safe caller message is used below. */ }
  if (!response.ok) {
    const error = new Error("Authenticated request failed");
    error.status = response.status;
    throw error;
  }
  return result;
}

export const getAuthStatus = user => authenticatedRequest(user, "/auth/status");
export const getCurrentProfile = user => authenticatedRequest(user, "/auth/me");

// Existing accounts need one authenticated profile read. The status endpoint is
// only needed when registration is incomplete or a rejection reason is shown.
export async function getSessionProfile(user) {
  if (!user.emailVerified) return { status: "unverified", email: user.email || "" };
  let profile;
  try {
    profile = await getCurrentProfile(user);
  } catch (error) {
    if (error.status === 404) return getAuthStatus(user);
    throw error;
  }
  if (profile.status === "rejected") {
    const state = await getAuthStatus(user);
    return { ...profile, decisionReason: state.decisionReason || "" };
  }
  return profile;
}

export const submitRegistration = (user, fields) => authenticatedRequest(user, "/auth/registration", {
  method: "POST", body: JSON.stringify(fields),
});
export const getAdminRequests = user => authenticatedRequest(user, "/auth/admin/requests");
export const decideAdminRequest = (user, uid, status) => authenticatedRequest(
  user, `/auth/admin/users/${encodeURIComponent(uid)}/decision`, {
    method: "POST",
    body: JSON.stringify({
      status,
      reason: status === "approved" ? "Approved by administrator" : "Rejected by administrator",
    }),
  },
);
