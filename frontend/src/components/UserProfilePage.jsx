import { APP_CONFIG } from "../config/appConfig";
import GearLogo from "./layout/GearLogo";
import { displayLabel, statusLabel } from "./profileUtils";

function Detail({ label, value }) {
  if (!value) return null;
  return <div className="profile-detail"><dt>{label}</dt><dd>{value}</dd></div>;
}

export default function UserProfilePage({ profile, loading, error, onBack, onLogout }) {
  return <main className="profile-page-shell">
    <header className="profile-page-header">
      <div className="header-brand"><GearLogo size={32} /><span className="header-title">{APP_CONFIG.appName}</span></div>
      <div className="profile-page-actions">
        <button type="button" className="profile-back" onClick={onBack}>← Back to Copilot</button>
        <button type="button" className="profile-page-logout" onClick={onLogout}>Log out</button>
      </div>
    </header>
    <div className="profile-page-content">
      <section className="profile-page-card" aria-labelledby="profile-title">
        <p className="profile-kicker">ACCOUNT</p>
        <h1 id="profile-title">My Profile</h1>
        <p className="profile-page-intro">Your account information is provided securely by your organization.</p>
        {loading && <p className="profile-state">Loading profile…</p>}
        {error && <p className="profile-error" role="alert">Unable to load your profile. Please refresh or sign in again.</p>}
        {!loading && !error && profile && <dl className="profile-details">
          <Detail label="Name" value={profile.name} />
          <Detail label="Email" value={profile.email} />
          <Detail label="Employee ID" value={profile.employee_id} />
          <Detail label="Company name" value={profile.company_name || profile.company_id} />
          <Detail label="Role" value={displayLabel(profile.role)} />
          <div className="profile-detail"><dt>Account status</dt><dd>
            <span className={`profile-status profile-status--${profile.status || "unknown"}`}>
              {statusLabel(profile.status)}
            </span>
          </dd></div>
        </dl>}
      </section>
    </div>
  </main>;
}
