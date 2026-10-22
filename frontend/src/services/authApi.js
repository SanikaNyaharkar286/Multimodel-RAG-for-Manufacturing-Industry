import { APP_CONFIG } from "../config/appConfig";

async function authenticatedRequest(user, path, options = {}) {
  async function request(forceRefresh = false) {
    const token = await user.getIdToken(forceRefresh);
    return fetch(`${APP_CONFIG.apiBaseUrl}${path}`, {
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
        Authorization: `Bearer ${token}`,
      },
    });
  }

  let response;
  try {
    response = await request(false);
  } catch (error) {
    // The API can still be starting while Firebase restores the browser
    // session. Give that first request one short, bounded retry.
    await new Promise(resolve => setTimeout(resolve, 150));
    response = await request(false).catch(() => { throw error; });
  }
  // A browser restored on another device can hold an expired/revoked cached
  // ID token. Refresh once before showing the user a profile-load failure.
  if (response.status === 401) response = await request(true);
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

function normalizeStatusProfile(profile, user) {
  if (!profile || typeof profile !== "object") return profile;
  return {
    uid: profile.uid,
    name: profile.name || profile.fullName,
    email: profile.email || user.email || "",
    employee_id: profile.employee_id || profile.employeeId,
    company_id: profile.company_id || profile.companyId,
    role: profile.role,
    status: profile.status,
    created_at: profile.created_at || profile.createdAt,
    decisionReason: profile.decisionReason || "",
  };
}

// Existing accounts need one authenticated profile read. The status endpoint is
// only needed when registration is incomplete or a rejection reason is shown.
export async function getSessionProfile(user) {
  if (!user.emailVerified) return { status: "unverified", email: user.email || "" };
  let profile;
  try {
    profile = await getCurrentProfile(user);
  } catch (error) {
    if (error.status === 404) return normalizeStatusProfile(await getAuthStatus(user), user);
    // Keep sign-in usable during a short /auth/me deployment or Firestore
    // read failure. Both calls use the verified Firebase token; status is not
    // a client-storage fallback and protected routes remain server-enforced.
    try {
      const status = await getAuthStatus(user);
      if (status?.status) return normalizeStatusProfile(status, user);
    } catch { /* Preserve the original profile error for the caller. */ }
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
