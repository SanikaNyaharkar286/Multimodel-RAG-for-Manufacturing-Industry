import GearLogo from "./GearLogo";
import { APP_CONFIG } from "../config/appConfig";

function Header({ onNewChat }) {
  return (
    <header className="header">
      <div className="header-brand">
        <GearLogo size={32} />
        <span className="header-title">{APP_CONFIG.appName}</span>
      </div>

      <button type="button" className="new-chat-btn" onClick={onNewChat}>
        + New chat
      </button>
    </header>
  );
}

export default Header;