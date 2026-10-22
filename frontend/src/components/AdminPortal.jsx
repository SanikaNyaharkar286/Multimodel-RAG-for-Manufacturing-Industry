import { useCallback, useEffect, useState } from 'react';
import { getAdminRequests, decideAdminRequest } from '../services/authApi';
import { APP_CONFIG } from '../config/appConfig';
import GearLogo from './layout/GearLogo';
import '../styles/admin.css';

export default function AdminPortal({ user, onClose, requestUid }) {
  const [requests, setRequests] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [deciding, setDeciding] = useState('');
  const [filter, setFilter] = useState(requestUid ? 'all' : 'pending');
  const [search, setSearch] = useState('');
  const load = useCallback(async () => {
    setLoading(true); setError('');
    try {
      const result = await getAdminRequests(user);
      setRequests([...(result.requests || [])].sort((a, b) => Number(b.uid === requestUid) - Number(a.uid === requestUid)));
    } catch { setError('Could not load approval requests. Please try again.'); }
    finally { setLoading(false); }
  }, [user, requestUid]);
  useEffect(() => {
    const initial = setTimeout(load, 0), refresh = setInterval(load, 30000);
    return () => { clearTimeout(initial); clearInterval(refresh); };
  }, [load]);
  async function decide(uid, status) {
    setDeciding(uid); setError('');
    try { await decideAdminRequest(user, uid, status); await load(); }
    catch { setError('Could not update this request. Please try again.'); }
    finally { setDeciding(''); }
  }
  const pending = requests.filter(request => request.status === 'pending').length;
  const visible = requests.filter(request => (filter === 'all' || request.status === 'pending') &&
    [request.fullName, request.email, request.department, request.employeeId].some(value => String(value || '').toLowerCase().includes(search.toLowerCase().trim())));
  return <main className="admin-page">
    <header className="admin-page-header"><div className="header-brand"><span className="brand-symbol"><GearLogo size={29}/></span>
      <div className="header-brand-copy"><span className="header-title">{APP_CONFIG.appName}</span><span className="header-subtitle">Administration</span></div></div>
      <button type="button" className="new-chat-btn" onClick={onClose}>← Back to chat</button></header>
    <div className="admin-page-content">
      <div className="admin-page-title"><div><p className="admin-eyebrow">ACCESS MANAGEMENT</p><h1>Technician approvals</h1><p>Review access requests for your maintenance workspace.</p></div>
        <button type="button" className="admin-refresh" disabled={loading || Boolean(deciding)} onClick={load}>{loading ? 'Refreshing…' : 'Refresh requests'}</button></div>
      <section className="admin-requests-panel" aria-label="Registration requests">
        <div className="admin-toolbar"><div className="admin-filters" aria-label="Filter requests">
          <button type="button" aria-pressed={filter === 'pending'} onClick={() => setFilter('pending')}>Pending <span>{pending}</span></button>
          <button type="button" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All requests <span>{requests.length}</span></button></div>
          <input type="search" aria-label="Search technician requests" placeholder="Search name, email, or employee ID" value={search} onChange={event => setSearch(event.target.value)}/></div>
        {error && <p className="admin-page-error" role="alert">{error}</p>}
        {loading && !requests.length ? <p className="admin-empty" role="status">Loading technician requests…</p>
          : !visible.length ? <div className="admin-empty"><h2>{search ? 'No matching requests' : filter === 'pending' ? 'No pending requests' : 'No requests yet'}</h2><p>{search ? 'Try a different name or email.' : 'New requests will appear here.'}</p></div>
          : <div className="admin-page-list">{visible.map(request => <article className={`admin-request${request.uid === requestUid ? ' admin-request--selected' : ''}`} key={request.uid}>
            <div className="admin-request-info">{request.uid === requestUid && <p className="admin-email-target">Opened from email</p>}
              <h2>{request.fullName || request.email}</h2><p>{request.email}</p>
              <div className="admin-request-meta"><span>{request.department || 'Department not provided'}</span><span>{request.site || 'Site not provided'}</span><span>Employee {request.employeeId || '—'}</span></div>
              <small>{request.requestReason || 'No reason provided'}</small></div>
            <div className="admin-request-result"><strong className={`admin-status admin-status--${request.status}`}>{request.status}</strong>
              {request.status === 'pending' && <div className="admin-actions"><button type="button" className="admin-approve" disabled={Boolean(deciding) || loading} onClick={() => decide(request.uid, 'approved')}>{deciding === request.uid ? 'Saving…' : 'Approve'}</button>
                <button type="button" className="admin-reject" disabled={Boolean(deciding) || loading} onClick={() => decide(request.uid, 'rejected')}>Reject</button></div>}</div>
          </article>)}</div>}
      </section>
    </div>
  </main>;
}
