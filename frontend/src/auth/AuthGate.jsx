import { useCallback, useEffect, useState } from "react";
import { onAuthStateChanged, signInWithEmailAndPassword, signOut,
  createUserWithEmailAndPassword, sendEmailVerification } from "firebase/auth";
import { firebaseAuth, firebaseConfigured, configureBrowserSession } from "./firebase";
import { APP_CONFIG } from "../config/appConfig";
import { approvalPortalState } from "./approvalLink";
import { AuthProfileProvider } from "./AuthProfileContext";
import UserProfileMenu from "../components/UserProfileMenu";
import UserProfilePage from "../components/UserProfilePage";
import { decideAdminRequest, getAdminRequests, getCurrentProfile, getSessionProfile,
  submitRegistration } from "../services/authApi";
import { cancelChatRequest, forgetStoredChatSession } from "../services/chatService";

export function AuthGate({ children }) {
  const enabled = import.meta.env.VITE_AUTH_ENABLED === "true";
  const [user, setUser] = useState(null);
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState(null);
  const [profileLoading, setProfileLoading] = useState(enabled);
  const [profileError, setProfileError] = useState(false);
  const [profileRetry, setProfileRetry] = useState(0);
  const [fields, setFields] = useState({ companyId: "", fullName: "", employeeId: "", department: "", site: "", designation: "", phone: "", requestReason: "" });
  const [approvalTarget] = useState(() => approvalPortalState(typeof window === "undefined" ? "" : window.location.search));
  const [adminOpen, setAdminOpen] = useState(approvalTarget.open);
  const [profilePageOpen, setProfilePageOpen] = useState(() =>
    typeof window !== "undefined" && window.location.hash === "#profile");

  useEffect(() => {
    if (!enabled || !firebaseAuth) return undefined;
    let active = true;
    configureBrowserSession();
    const unsubscribe = onAuthStateChanged(firebaseAuth, async nextUser => {
      if (!active) return;
      // Firebase can restore a cached user before its verification flag is
      // refreshed. Reload it so verification completed on another device is
      // recognized immediately.
      if (nextUser) {
        try { await nextUser.reload(); } catch { /* The normal auth flow handles a transient reload failure. */ }
      }
      setUser(nextUser);
      setProfile(null);
      setProfileError(false);
      if (!nextUser) { setProfileLoading(false); return; }
      setProfileLoading(true);
      try {
        const nextProfile = await getSessionProfile(nextUser);
        if (active) setProfile(nextProfile);
      } catch {
        if (active) setProfileError(true);
      } finally {
        if (active) setProfileLoading(false);
      }
    });
    return () => { active = false; unsubscribe(); };
  }, [enabled, profileRetry]);

  useEffect(() => {
    if (!user || profile?.status !== "pending") return undefined;
    let active = true;
    const timer = setInterval(async () => {
      try {
        const state = await getSessionProfile(user);
        if (active && state.status !== "pending") setProfile(state);
      } catch { /* Keep the pending screen until a status check succeeds. */ }
    }, 10000);
    return () => { active = false; clearInterval(timer); };
  }, [user, profile?.status]);

  useEffect(() => {
    const syncProfilePage = () => setProfilePageOpen(window.location.hash === "#profile");
    window.addEventListener("hashchange", syncProfilePage);
    window.addEventListener("popstate", syncProfilePage);
    return () => {
      window.removeEventListener("hashchange", syncProfilePage);
      window.removeEventListener("popstate", syncProfilePage);
    };
  }, []);

  const openProfile = useCallback(() => {
    window.history.pushState(null, "", `${window.location.pathname}${window.location.search}#profile`);
    setProfilePageOpen(true);
  }, []);
  const closeProfile = useCallback(() => {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    setProfilePageOpen(false);
  }, []);
  const logout = useCallback(() => {
    cancelChatRequest();
    forgetStoredChatSession();
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    setProfilePageOpen(false);
    return signOut(firebaseAuth);
  }, []);

  if (!enabled) return children;
  if (!firebaseConfigured) return <AuthMessage title="Authentication setup required"
    text="Set the Firebase web-app values before enabling login." />;
  if (user && profileLoading) return <AuthMessage title="Loading profile…"
    text="Please wait while your account details are loaded." />;
  if (user && profileError) return <AuthMessage title="Unable to load your profile"
    text="Unable to load your profile. Please refresh or sign in again."
    onRetry={() => setProfileRetry(value => value + 1)} onLogout={logout} />;
  if (user && profile && profilePageOpen && ["approved", "pending", "rejected", "suspended"].includes(profile.status))
    return <UserProfilePage profile={profile} onBack={closeProfile} onLogout={logout} />;
  if (user && profile?.status === "pending") return <RestrictedAccount profile={profile}
    title="Approval pending" text="Your request is waiting for administrator approval."
    onLogout={logout} onProfileOpen={openProfile} />;
  if (user && profile?.status === "rejected") return <RestrictedAccount profile={profile}
    title="Request not approved" text={profile.decisionReason || "Please contact your administrator."}
    onLogout={logout} onProfileOpen={openProfile} />;
  if (user && profile?.status === "suspended") return <RestrictedAccount profile={profile}
    title="Access suspended" text="Contact your administrator for help."
    onLogout={logout} onProfileOpen={openProfile} />;
  if (user && profile?.status === "admin_setup_required") return <AuthMessage title="Admin setup required"
    text="This account is configured as the administrator. Run the one-time backend bootstrap command, then sign in again."
    command={`python -m auth_system.bootstrap_admin --email ${user.email}`} onLogout={logout} />;
  if (user && profile?.status === "unverified") return <VerificationRequired user={user}
    onVerified={async () => {
      try {
        await user.reload();
        await user.getIdToken(true);
        const refreshed = await getSessionProfile(user);
        setProfile(refreshed);
        setProfileError(false);
      } catch {
        setProfileError(true);
      }
    }} onLogout={logout} />;
  if (user && profile?.status === "incomplete") return <RegistrationForm user={user} fields={fields}
    setFields={setFields} setProfile={setProfile} />;
  if (user && profile?.status === "approved") {
    const context = { profile, profileLoading, profileError, logout,
      openProfile,
      openAdmin: profile.role === "admin" ? () => setAdminOpen(true) : null };
    return <AuthProfileProvider value={context}>{children}
      {adminOpen && profile.role === "admin" && <AdminPortal user={user}
        requestUid={approvalTarget.requestUid} onClose={() => setAdminOpen(false)} />}
    </AuthProfileProvider>;
  }

  async function submit(event) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      if (mode === "login") {
        const result = await signInWithEmailAndPassword(firebaseAuth, email, password);
        await result.user.reload();
        await result.user.getIdToken(true);
        if (!result.user.emailVerified) {
          // A login from a second device must have a way to obtain a fresh
          // link. Firebase rejects repeated sends only when rate limited; the
          // safe message below still leaves the account signed in to retry.
          try { await sendEmailVerification(result.user); }
          catch { /* The verification screen provides a retry button. */ }
        }
      }
      else {
        const result = await createUserWithEmailAndPassword(firebaseAuth, email, password);
        await sendEmailVerification(result.user);
        setError("Check your company email to verify the account, then sign in again.");
      }
    } catch (authError) {
      const messages = {
        "auth/operation-not-allowed": "Email/password sign-in is not enabled in Firebase Authentication.",
        "auth/invalid-api-key": "The Firebase web API key is invalid.",
        "auth/configuration-not-found": "The Firebase project is not configured for Authentication.",
        "auth/email-already-in-use": "This email is already registered. Sign in instead.",
        "auth/weak-password": "Use a stronger password with at least 8 characters.",
      };
      setError(messages[authError?.code] || "We could not complete that request. Check your details and try again.");
    } finally { setBusy(false); }
  }

  return <main className="auth-shell"><section className="auth-card">
    <div className="auth-brand"><span className="auth-brand-mark">⚙</span><span>{APP_CONFIG.appName}</span></div>
    <div className="auth-kicker">SECURE TECHNICIAN ACCESS</div>
    <h1>{mode === "login" ? "Welcome back" : "Request access"}</h1>
    <p className="auth-subtitle">{mode === "login" ? "Sign in to continue to your maintenance workspace." : "Create your technician account to request approval."}</p>
    <form onSubmit={submit} className="auth-form">
      <label>Email address<input type="email" placeholder="name@company.com" value={email} onChange={event => setEmail(event.target.value)} required /></label>
      <label>Password<input type="password" placeholder="At least 8 characters" value={password} onChange={event => setPassword(event.target.value)} minLength={8} required /></label>
      <button className="auth-primary" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
    </form>
    {error && <p className="auth-message" role="alert">{error}</p>}
    <button className="auth-switch" type="button" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
      {mode === "login" ? "Need access? Create an account" : "Already registered? Sign in"}
    </button>
  </section></main>;
}

function VerificationRequired({ user, onVerified, onLogout }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function resend() {
    setBusy(true); setMessage("");
    try {
      await sendEmailVerification(user);
      setMessage(`A new verification email was sent to ${user.email}. Check spam or junk folders too.`);
    } catch (error) {
      setMessage(error?.code === "auth/too-many-requests"
        ? "Please wait a few minutes before requesting another verification email."
        : "We could not send the verification email. Check your Firebase email settings and try again.");
    } finally { setBusy(false); }
  }
  async function checkAgain() {
    setBusy(true); setMessage("");
    try { await user.reload(); await user.getIdToken(true); await onVerified(); }
    catch { setMessage("We could not refresh your verification state. Please try again."); }
    finally { setBusy(false); }
  }
  return <AuthMessage title="Verify your email"
    text={`Verify the link sent to ${user.email}, then choose “I have verified” below.`}
    onLogout={onLogout}>
    <div className="verification-actions">
      <button className="auth-primary" onClick={resend} disabled={busy}>Resend verification email</button>
      <button className="auth-switch" onClick={checkAgain} disabled={busy}>I have verified — check again</button>
      {message && <p className="auth-message" role="status">{message}</p>}
    </div>
  </AuthMessage>;
}

function AuthMessage({ title, text, command, onRetry, onLogout, children }) {
  return <main className="auth-shell"><section className="auth-card">
    <div className="auth-brand"><span className="auth-brand-mark">⚙</span><span>{APP_CONFIG.appName}</span></div>
    <div className="auth-kicker">ACCOUNT</div><h1>{title}</h1><p className="auth-subtitle">{text}</p>
    {command && <code className="admin-command">{command}</code>}
    {children}
    {onRetry && <button className="auth-primary" onClick={onRetry}>Try again</button>}
    {onLogout && <button className="auth-switch" onClick={onLogout}>Sign out</button>}
  </section></main>;
}

function RestrictedAccount({ profile, title, text, onLogout, onProfileOpen }) {
  return <main className="auth-shell">
    <div className="restricted-profile"><UserProfileMenu profile={profile} onLogout={onLogout}
      onProfileOpen={onProfileOpen} /></div>
    <section className="auth-card"><div className="auth-kicker">ACCESS REQUEST</div>
      <h1>{title}</h1><p className="auth-subtitle">{text}</p>
    </section>
  </main>;
}

function AdminPortal({ user, onClose, requestUid }) {
  const [requests, setRequests] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setError("");
    try {
      const result = await getAdminRequests(user);
      setRequests([...(result.requests || [])].sort((a, b) => Number(b.uid === requestUid) - Number(a.uid === requestUid)));
    } catch { setError("Could not load approval requests. Please try again."); }
  }, [user, requestUid]);
  useEffect(() => {
    const initial = setTimeout(load, 0);
    const refresh = setInterval(load, 30000);
    return () => { clearTimeout(initial); clearInterval(refresh); };
  }, [load]);
  async function decide(uid, status) {
    setBusy(true); setError("");
    try { await decideAdminRequest(user, uid, status); await load(); }
    catch { setError("Could not update this request. Please try again."); }
    finally { setBusy(false); }
  }
  return <main className="admin-overlay"><section className="admin-card">
    <button className="admin-close" onClick={onClose}>Close</button>
    <div className="auth-brand"><span className="auth-brand-mark">⚙</span><span>{APP_CONFIG.appName}</span></div>
    <div className="auth-kicker">ADMINISTRATION</div><h1>Technician approvals</h1>
    <p className="auth-subtitle">Review registration requests before granting chatbot access.</p>
    <button className="auth-switch" onClick={load} disabled={busy}>Refresh requests</button>
    {error && <p className="auth-message" role="alert">{error}</p>}
    <div className="admin-list">{requests.length === 0 ? <p className="auth-subtitle">No technician requests yet.</p> : requests.map(request =>
      <article className={`admin-request${request.uid === requestUid ? " admin-request--selected" : ""}`} key={request.uid}>
        <div>{request.uid === requestUid && <p>Request opened from email</p>}<h2>{request.fullName || request.email}</h2>
          <p>{request.email} · {request.department || "Department not provided"}</p>
          <p>{request.site || "Site not provided"} · Employee {request.employeeId || "—"}</p>
          <small>{request.requestReason || "No reason provided"}</small>
          <strong className={`admin-status admin-status--${request.status}`}>{request.status}</strong>
        </div>
        {request.status === "pending" && <div className="admin-actions">
          <button className="admin-approve" disabled={busy} onClick={() => decide(request.uid, "approved")}>Approve</button>
          <button className="admin-reject" disabled={busy} onClick={() => decide(request.uid, "rejected")}>Reject</button>
        </div>}
      </article>)}</div>
  </section></main>;
}

function RegistrationForm({ user, fields, setFields, setProfile }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const update = event => setFields(value => ({ ...value, [event.target.name]: event.target.value }));
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await submitRegistration(user, fields);
      try { setProfile(await getCurrentProfile(user)); }
      catch { setError("Your request was submitted, but your profile could not be loaded. Please sign in again."); }
    } catch { setError("Registration failed. Please check your details and try again."); }
    finally { setBusy(false); }
  }
  const labels = [["companyId", "Company ID"], ["fullName", "Full name"], ["employeeId", "Employee ID"],
    ["department", "Department"], ["site", "Plant / site"], ["designation", "Job designation"],
    ["phone", "Phone (optional)"], ["requestReason", "Reason for access"]];
  return <main className="auth-shell"><section className="auth-card">
    <div className="auth-kicker">REGISTRATION DETAILS</div><h1>Complete your request</h1>
    <p className="auth-subtitle">These details are reviewed by an administrator.</p>
    <form className="auth-form" onSubmit={submit}>{labels.map(([name, label]) =>
      <label key={name}>{label}<input name={name} value={fields[name]} onChange={update} required={name !== "phone"} /></label>)}
      <button className="auth-primary" disabled={busy}>{busy ? "Submitting…" : "Submit for approval"}</button>
    </form>{error && <p className="auth-message" role="alert">{error}</p>}
  </section></main>;
}
