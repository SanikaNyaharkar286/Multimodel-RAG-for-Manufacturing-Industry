import { useEffect, useRef, useState } from "react";
import { displayLabel, profileName } from "./profileUtils";

function initials(profile) {
  const source = profile?.name || profile?.email || "U";
  return source.split(/[\s@]+/).filter(Boolean).slice(0, 2).map(part => part[0].toUpperCase()).join("");
}

export default function UserProfileMenu({ profile, loading = false, error = false,
  onLogout, onAdminOpen, onProfileOpen }) {
  const [open, setOpen] = useState(false);
  const root = useRef(null);

  useEffect(() => {
    const close = event => {
      if (event.key === "Escape") setOpen(false);
      if (event.type === "mousedown" && root.current && !root.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, []);

  return <div className="profile-menu" ref={root}>
    <button type="button" className="profile-trigger" onClick={() => setOpen(value => !value)}
      aria-expanded={open} aria-haspopup="menu">
      <span className="profile-avatar" aria-hidden="true">{initials(profile)}</span>
      <span className="profile-trigger-copy">
        <strong>{loading ? "Loading profile…" : profileName(profile)}</strong>
        <small>{error ? "Profile unavailable" : displayLabel(profile?.role)}</small>
      </span>
      <span className="profile-chevron" aria-hidden="true">⌄</span>
    </button>
    {open && <div className="profile-dropdown" role="menu">
      <button type="button" role="menuitem" onClick={() => { onProfileOpen?.(); setOpen(false); }}>
        My Profile
      </button>
      {profile?.role === "admin" && onAdminOpen && <button type="button" role="menuitem"
        onClick={() => { onAdminOpen(); setOpen(false); }}>Admin portal</button>}
      <button type="button" role="menuitem" className="profile-logout"
        onClick={onLogout}>Log out</button>
    </div>}
  </div>;
}
