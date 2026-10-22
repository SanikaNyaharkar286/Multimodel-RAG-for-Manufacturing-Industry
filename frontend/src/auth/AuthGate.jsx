import { useEffect, useState } from "react";
import {
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
  createUserWithEmailAndPassword,
  sendEmailVerification,
  reload,
} from "firebase/auth";
import { firebaseAuth, firebaseConfigured, useBrowserSession } from "./firebase";
import { APP_CONFIG } from "../config/appConfig";

export function AuthGate({ children }) {
  const enabled = import.meta.env.VITE_AUTH_ENABLED === "true";
  const [user, setUser] = useState(null);
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState(null);
  const [fields, setFields] = useState({ companyId: "", fullName: "", employeeId: "", department: "", site: "", designation: "", phone: "", requestReason: "" });
  const [adminOpen, setAdminOpen] = useState(false);

  useEffect(() => {
    if (!enabled || !firebaseAuth) return undefined;
    useBrowserSession();
    return onAuthStateChanged(firebaseAuth, async nextUser => {
      setUser(nextUser);
      if (!nextUser) { setProfile(null); return; }
      await reload(nextUser);

      const token = await nextUser.getIdToken(true);

      const response = await fetch(
        `${APP_CONFIG.apiBaseUrl}/auth/status`,
        { headers: { Authorization: `Bearer ${token}` } }
      );

      if (response.ok) {
        setProfile(await response.json());
      } else {
        const details = await response.text();
        console.error(
          "[AUTH] Status request failed:",
          response.status,
          details
        );
        setProfile(null);
      }
    });
  }, [enabled]);

  if (!enabled) return children;
  if (!firebaseConfigured) return <main className="auth-shell"><div className="auth-card"><div className="auth-brand"><span className="auth-brand-mark">⚙</span><span>Manufacturing Equipment Copilot</span></div><h1>Authentication setup required</h1><p>Set the Firebase web-app values before enabling login.</p></div></main>;
  if (user && profile?.status === "pending") return <main className="auth-shell"><section className="auth-card"><div className="auth-kicker">ACCESS REQUEST</div><h1>Approval pending</h1><p className="auth-subtitle">Your request is waiting for administrator approval.</p><button className="auth-switch" onClick={() => signOut(firebaseAuth)}>Sign out</button></section></main>;
  if (user && profile?.status === "rejected") return <main className="auth-shell"><section className="auth-card"><div className="auth-kicker">ACCESS REQUEST</div><h1>Request not approved</h1><p className="auth-subtitle">{profile.decisionReason || "Please contact your administrator."}</p><button className="auth-switch" onClick={() => signOut(firebaseAuth)}>Sign out</button></section></main>;
  if (user && profile?.status === "suspended") return <main className="auth-shell"><section className="auth-card"><div className="auth-kicker">ACCOUNT SUSPENDED</div><h1>Access suspended</h1><p className="auth-subtitle">Contact your administrator for help.</p><button className="auth-switch" onClick={() => signOut(firebaseAuth)}>Sign out</button></section></main>;
  if (user && profile?.status === "admin_setup_required") return <main className="auth-shell"><section className="auth-card"><div className="auth-kicker">ADMIN ACCOUNT</div><h1>Admin setup required</h1><p className="auth-subtitle">This account is configured as the administrator. Run the one-time backend bootstrap command, then sign in again.</p><code className="admin-command">python -m auth_system.bootstrap_admin --email {user.email}</code><button className="auth-switch" onClick={() => signOut(firebaseAuth)}>Sign out</button></section></main>;
  if (user && !profile) return <main className="auth-shell"><section className="auth-card"><div className="auth-kicker">EMAIL VERIFICATION</div><h1>Verify your email</h1><p className="auth-subtitle">Verify the link sent to {user.email}, then sign in again.</p><button className="auth-switch" onClick={() => signOut(firebaseAuth)}>Sign out</button></section></main>;
  if (user && profile?.status === "incomplete") return <RegistrationForm user={user} fields={fields} setFields={setFields} setProfile={setProfile} />;
  if (user && profile?.status === "approved" && profile?.role === "admin") return <><>{children}</><button className="auth-admin" onClick={() => setAdminOpen(true)}>Admin portal</button><button className="auth-logout" onClick={() => signOut(firebaseAuth)}>Log out</button>{adminOpen && <AdminPortal user={user} onClose={() => setAdminOpen(false)} />}</>;
  if (user && profile?.status === "approved") return <><>{children}</><button className="auth-logout" onClick={() => signOut(firebaseAuth)}>Log out</button></>;

  async function submit(event) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      if (mode === "login") await signInWithEmailAndPassword(firebaseAuth, email, password);
      else { const result = await createUserWithEmailAndPassword(firebaseAuth, email, password); await sendEmailVerification(result.user); setError("Check your company email to verify the account, then sign in again."); }
    } catch (authError) {
      const code = authError?.code || "";
      const messages = {
        "auth/operation-not-allowed": "Email/password sign-in is not enabled in Firebase Authentication.",
        "auth/invalid-api-key": "The Firebase web API key is invalid.",
        "auth/configuration-not-found": "The Firebase project is not configured for Authentication.",
        "auth/email-already-in-use": "This email is already registered. Sign in instead.",
        "auth/weak-password": "Use a stronger password with at least 8 characters.",
      };
      setError(messages[code] || "We could not complete that request. Check your details and try again.");
    }
    finally { setBusy(false); }
  }

  return <main className="auth-shell"><section className="auth-card"><div className="auth-brand"><span className="auth-brand-mark">⚙</span><span>{APP_CONFIG.appName}</span></div><div className="auth-kicker">SECURE TECHNICIAN ACCESS</div><h1>{mode === "login" ? "Welcome back" : "Request access"}</h1><p className="auth-subtitle">{mode === "login" ? "Sign in to continue to your maintenance workspace." : "Create your technician account to request approval."}</p>
    <form onSubmit={submit} className="auth-form"><label>Email address<input type="email" placeholder="name@company.com" value={email} onChange={e => setEmail(e.target.value)} required /></label>
      <label>Password<input type="password" placeholder="At least 8 characters" value={password} onChange={e => setPassword(e.target.value)} minLength={8} required /></label>
      <button className="auth-primary" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button></form>
    {error && <p className="auth-message" role="alert">{error}</p>}<button className="auth-switch" type="button" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>{mode === "login" ? "Need access? Create an account" : "Already registered? Sign in"}</button></section></main>;
}

function AdminPortal({ user, onClose }) {
  const [requests, setRequests] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function load() {
    setError("");
    try {
      const token = await user.getIdToken();
      const response = await fetch(`${APP_CONFIG.apiBaseUrl}/auth/admin/requests`, { headers: { Authorization: `Bearer ${token}` } });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Could not load requests.");
      setRequests(result.requests || []);
    } catch (err) { setError(err.message); }
  }
  useEffect(() => { load(); }, []);
  async function decide(uid, status) {
    setBusy(true); setError("");
    try {
      const token = await user.getIdToken();
      const response = await fetch(`${APP_CONFIG.apiBaseUrl}/auth/admin/users/${uid}/decision`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` }, body: JSON.stringify({ status, reason: status === "approved" ? "Approved by administrator" : "Rejected by administrator" }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Could not update request.");
      await load();
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  return <main className="admin-overlay"><section className="admin-card"><button className="admin-close" onClick={onClose}>Close</button><div className="auth-brand"><span className="auth-brand-mark">⚙</span><span>Manufacturing Equipment Copilot</span></div><div className="auth-kicker">ADMINISTRATION</div><h1>Technician approvals</h1><p className="auth-subtitle">Review registration requests before granting chatbot access.</p>{error && <p className="auth-message" role="alert">{error}</p>}<div className="admin-list">{requests.length === 0 ? <p className="auth-subtitle">No technician requests yet.</p> : requests.map(request => <article className="admin-request" key={request.uid}><div><h2>{request.fullName || request.email}</h2><p>{request.email} · {request.department || "Department not provided"}</p><p>{request.site || "Site not provided"} · Employee {request.employeeId || "—"}</p><small>{request.requestReason || "No reason provided"}</small><strong className={`admin-status admin-status--${request.status}`}>{request.status}</strong></div>{request.status === "pending" && <div className="admin-actions"><button className="admin-approve" disabled={busy} onClick={() => decide(request.uid, "approved")}>Approve</button><button className="admin-reject" disabled={busy} onClick={() => decide(request.uid, "rejected")}>Reject</button></div>}</article>)}</div></section></main>;
}

function RegistrationForm({ user, fields, setFields, setProfile }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const update = event => setFields(value => ({ ...value, [event.target.name]: event.target.value }));
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const token = await user.getIdToken();
      const response = await fetch(`${APP_CONFIG.apiBaseUrl}/auth/registration`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` }, body: JSON.stringify(fields) });
      const result = await response.json(); if (!response.ok) throw new Error(result.detail || "Registration failed"); setProfile(result);
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  const labels = [["companyId", "Company ID"], ["fullName", "Full name"], ["employeeId", "Employee ID"], ["department", "Department"], ["site", "Plant / site"], ["designation", "Job designation"], ["phone", "Phone (optional)"], ["requestReason", "Reason for access"]];
  return <main className="auth-shell"><section className="auth-card"><div className="auth-kicker">REGISTRATION DETAILS</div><h1>Complete your request</h1><p className="auth-subtitle">These details are reviewed by an administrator.</p><form className="auth-form" onSubmit={submit}>{labels.map(([name, label]) => <label key={name}>{label}<input name={name} value={fields[name]} onChange={update} required={!['phone'].includes(name)} /></label>)}<button className="auth-primary" disabled={busy}>{busy ? "Submitting…" : "Submit for approval"}</button></form>{error && <p className="auth-message" role="alert">{error}</p>}</section></main>;
}
