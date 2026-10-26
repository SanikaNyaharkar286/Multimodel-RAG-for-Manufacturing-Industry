import { useState } from 'react';
import GearLogo from "./GearLogo";
import { APP_CONFIG } from "../../config/appConfig";
import UserProfileMenu from "../UserProfileMenu";
import { useAuthProfile } from "../../auth/AuthProfileContext";
import WorkspaceIcon from './WorkspaceIcon';

function Header({ onNewChat, showNewChat, onChatOpen, onReportsOpen }) {
  const auth = useAuthProfile();
  const [activeSection, setActiveSection] = useState('chat');
  const [notice, setNotice] = useState('');

  function openChat() {
    setActiveSection('chat');
    setNotice('');
    onChatOpen?.();
  }

  function openReports() {
    const opened = onReportsOpen?.();
    if (opened) {
      setActiveSection('reports');
      setNotice('');
    } else {
      setNotice('A report will appear here after it is created from this conversation.');
    }
  }

  return (
    <header className="header">
      <button type="button" className="header-brand" onClick={openChat} aria-label="Go to Copilot chat">
        <span className="brand-symbol"><GearLogo size={29} /></span>
        <div className="header-brand-copy"><span className="header-title">{APP_CONFIG.appName}</span><span className="header-subtitle">Equipment maintenance workspace</span></div>
      </button>

      <nav className="header-nav" aria-label="Workspace navigation">
        <button type="button" className={`header-nav-item ${activeSection === 'chat' ? 'is-active' : ''}`}
          aria-current={activeSection === 'chat' ? 'page' : undefined} onClick={openChat}>
          <WorkspaceIcon name="chat" size={18}/><span>Chat</span>
        </button>
        <button type="button" className={`header-nav-item ${activeSection === 'reports' ? 'is-active' : ''}`}
          aria-current={activeSection === 'reports' ? 'page' : undefined} onClick={openReports}>
          <WorkspaceIcon name="reports" size={18}/><span>Reports</span>
        </button>
        {auth?.profile?.role === 'admin' && auth.openAdmin && <button type="button" className="header-nav-item"
          onClick={auth.openAdmin}><WorkspaceIcon name="admin" size={18}/><span>Admin</span></button>}
      </nav>

      <div className="header-actions">
        <button type="button" className="new-chat-btn" onClick={() => {
          setActiveSection('chat');
          setNotice('');
          if (showNewChat) onNewChat?.(); else onChatOpen?.();
        }}>
          + New chat
        </button>
        {auth && <UserProfileMenu profile={auth.profile} loading={auth.profileLoading}
          error={auth.profileError} onLogout={auth.logout} onAdminOpen={auth.openAdmin}
          onProfileOpen={auth.openProfile} />}
      </div>
      {notice && <p className="header-nav-notice" role="status">{notice}</p>}
    </header>
  );
}

export default Header;
