import GearLogo from "./GearLogo";
import { APP_CONFIG } from "../../config/appConfig";
import UserProfileMenu from "../UserProfileMenu";
import { useAuthProfile } from "../../auth/AuthProfileContext";

function Header({ onNewChat, showNewChat }) {
  const auth = useAuthProfile();
  return (
    <header className="header">
      <div className="header-brand">
        <span className="brand-symbol"><GearLogo size={29} /></span>
        <div className="header-brand-copy"><span className="header-title">{APP_CONFIG.appName}</span><span className="header-subtitle">Equipment maintenance workspace</span></div>
      </div>

      <div className="header-actions">
        {showNewChat && <button type="button" className="new-chat-btn" onClick={onNewChat}>
          + New chat
        </button>}
        {auth && <UserProfileMenu profile={auth.profile} loading={auth.profileLoading}
          error={auth.profileError} onLogout={auth.logout} onAdminOpen={auth.openAdmin}
          onProfileOpen={auth.openProfile} />}
      </div>
    </header>
  );
}

export default Header;
