import { useCallback, useEffect, useRef, useState } from 'react';
import { getAdminRequests, decideAdminRequest } from '../services/authApi';
import { APP_CONFIG } from '../config/appConfig';
import GearLogo from './layout/GearLogo';
import '../styles/admin.css';
import AdminReports from './reports/AdminReports';
import AdminNotifications from './reports/AdminNotifications';
import { adminNotifications, markNotificationRead, markAllNotificationsRead } from '../services/reportSubmissionsApi';
import { listAdminMachines, uploadAdminFiles } from '../services/adminFilesApi';
import AdminFilesModal from './AdminFilesModal';

function RegistrationRequests({ user, requestUid }) {
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
  return <section aria-label="User Requests">
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
  </section>;
}

export default function AdminPortal({ user, onClose, requestUid = '', initialReportId = '', initialTab = 'requests' }) {
  const [tab, setTab] = useState(initialReportId ? 'reports' : requestUid ? 'requests' : initialTab);
  const [reportId, setReportId] = useState(initialReportId);
  const [requestId, setRequestId] = useState(requestUid);
  const [bellOpen, setBellOpen] = useState(false);
  const [inbox, setInbox] = useState(null);
  const [error, setError] = useState('');
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [addFilesOpen, setAddFilesOpen] = useState(false);
  const [addFilesMode, setAddFilesMode] = useState('choose');
  const [machineSearch, setMachineSearch] = useState('');
  const [selectedMachine, setSelectedMachine] = useState('');
  const [newMachineName, setNewMachineName] = useState('');
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [existingMachines, setExistingMachines] = useState([]);
  const [machinesLoading, setMachinesLoading] = useState(false);
  const [filesBusy, setFilesBusy] = useState(false);
  const [filesError, setFilesError] = useState('');
  const inboxVersion = useRef(0);
  const loadInbox = useCallback(async () => {
    const version = ++inboxVersion.current;
    try { const value = await adminNotifications(page); if (version === inboxVersion.current) { setInbox(value); setError(''); } }
    catch (err) { if (version === inboxVersion.current) setError(err.message); }
  }, [page]);
  useEffect(() => { loadInbox(); const timer = setInterval(loadInbox, 20000); return () => { clearInterval(timer); inboxVersion.current++; }; }, [loadInbox]);
  function navigate(next, id = '') {
    setTab(next); setReportId(next === 'reports' ? id : ''); setBellOpen(false);
    if (next === 'requests' && id) setRequestId(id);
    const params = new URLSearchParams(window.location.search);
    params.set('admin', next === 'requests' ? 'approvals' : next); params.delete('report'); params.delete('request');
    if (next === 'reports' && id) params.set('report', id);
    if (next === 'requests' && id) params.set('request', id);
    window.history.replaceState(null, '', `${window.location.pathname}?${params}#admin`);
  }
  async function markRead(id) {
    setBusy(true); setError('');
    try { await markNotificationRead(id); await loadInbox(); }
    catch (err) { setError(err.message); throw err; }
    finally { setBusy(false); }
  }
  async function markAll() {
    setBusy(true); setError('');
    try { await markAllNotificationsRead(); await loadInbox(); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  async function openNotification(item) {
    try {
      if (!item.is_read) await markRead(item.notification_id);
      if (item.type === 'maintenance_report_submitted') navigate('reports', item.report_id);
      else navigate('requests', item.request_uid);
    } catch { /* Keep the inbox visible with its actionable error. */ }
  }
  const notifications = <AdminNotifications data={inbox} error={error} busy={busy} page={page} onPage={setPage}
    onRefresh={loadInbox}
    onOpen={openNotification} onRead={id => { markRead(id).catch(() => {}); }} onReadAll={markAll} />;
  const visibleMachines = existingMachines.filter(machine => machine.toLowerCase().includes(machineSearch.trim().toLowerCase()));
  const loadMachines = useCallback(async () => {
    setMachinesLoading(true); setFilesError('');
    try { const result = await listAdminMachines(); setExistingMachines(result.machines || []); }
    catch (err) { setFilesError(err.message || 'Could not load machines from the raw bucket.'); }
    finally { setMachinesLoading(false); }
  }, []);
  function chooseFiles(event) { setSelectedFiles(Array.from(event.target.files || [])); }
  async function submitFiles() {
    const machine = addFilesMode === 'machine' ? newMachineName.trim() : selectedMachine;
    if (!machine || !selectedFiles.length) return;
    setFilesBusy(true); setFilesError('');
    try { await uploadAdminFiles(machine, selectedFiles); closeAddFiles(); }
    catch (err) { setFilesError(err.message || 'Could not upload the manuals.'); }
    finally { setFilesBusy(false); }
  }
  function closeAddFiles() {
    setAddFilesOpen(false); setAddFilesMode('choose'); setMachineSearch(''); setSelectedMachine(''); setNewMachineName(''); setSelectedFiles([]); setFilesError('');
  }
  return <main className="admin-page">
    <header className="admin-page-header"><div className="header-brand"><span className="brand-symbol"><GearLogo size={29}/></span>
      <div className="header-brand-copy"><span className="header-title">{APP_CONFIG.appName}</span><span className="header-subtitle">Administration</span></div></div>
      <div className="admin-header-actions"><button type="button" className="admin-add-files" onClick={() => setAddFilesOpen(true)}><span aria-hidden="true">+</span> Add files</button><button className="notification-bell" type="button" aria-label={`Notifications${inbox ? `, ${inbox.unread_count} unread` : ''}`} aria-expanded={bellOpen} aria-controls="admin-notification-panel" onClick={() => setBellOpen(value => !value)}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></svg>
        {Boolean(inbox?.unread_count) && <span className="notification-badge">{inbox.unread_count}</span>}</button>
        <button type="button" className="new-chat-btn" onClick={onClose}>← Back to chat</button></div></header>
    <nav className="admin-navigation" aria-label="Admin navigation">{[['dashboard','Dashboard'],['requests','User Requests'],['reports','Maintenance Reports'],['notifications','Notifications']].map(([value, label]) =>
      <button key={value} type="button" aria-current={tab === value ? 'page' : undefined} onClick={() => navigate(value)}>{label}</button>)}</nav>
    {bellOpen && <div id="admin-notification-panel" className="admin-notification-panel"><button type="button" onClick={() => setBellOpen(false)}>Close notifications</button>{notifications}</div>}
    {tab === 'requests' ? <RegistrationRequests user={user} requestUid={requestId} /> : <div className="admin-page-content">
      {tab === 'notifications' ? notifications : <AdminReports key={tab} dashboard={tab === 'dashboard'} selectedId={reportId} onSelect={id => navigate('reports', id)} onOpenReports={() => navigate('reports')} />}
    </div>}
    {error && !bellOpen && tab !== 'notifications' && <p role="alert" className="admin-page-error">Notifications could not be refreshed. {error} <button type="button" onClick={loadInbox}>Retry notifications</button></p>}
    {false && addFilesOpen && <div className="admin-modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) closeAddFiles(); }}>
      <section className="admin-add-files-modal" role="dialog" aria-modal="true" aria-labelledby="add-files-title">
        <div className="admin-modal-heading"><div><p className="admin-eyebrow">KNOWLEDGE LIBRARY</p><h2 id="add-files-title">Add files</h2><p>Choose how you want to expand your machine documentation.</p></div><button type="button" className="admin-modal-close" aria-label="Close Add files" onClick={closeAddFiles}>×</button></div>
        {addFilesMode === 'choose' && <div className="admin-add-options"><button type="button" className="admin-add-option" onClick={() => setAddFilesMode('machine')}><span className="admin-add-option-icon" aria-hidden="true">⚙</span><span><strong>Add a new machine</strong><small>Create a machine record, then attach its manuals.</small></span><span aria-hidden="true">→</span></button>
          <button type="button" className="admin-add-option" onClick={() => setAddFilesMode('document')}><span className="admin-add-option-icon admin-add-option-icon--blue" aria-hidden="true">▤</span><span><strong>Add a document</strong><small>Upload a manual for an existing machine.</small></span><span aria-hidden="true">→</span></button></div>}
        {addFilesMode === 'machine' && <div className="admin-add-form"><button type="button" className="admin-modal-back" onClick={() => setAddFilesMode('choose')}>← Back to options</button><label>Machine name<input type="text" placeholder="e.g. Production press 01" /></label><label>Machine model or identifier<input type="text" placeholder="Optional model or asset ID" /></label><div className="admin-file-drop"><span aria-hidden="true">↑</span><strong>Drop manuals here</strong><small>PDF, DOCX, or TXT · UI preview only</small><input type="file" multiple aria-label="Choose machine manuals" onChange={event => setSelectedFile(event.target.files?.[0] || null)} />{selectedFile && <em>{selectedFile.name}</em>}</div><button type="button" className="admin-primary-action" onClick={closeAddFiles}>Save machine</button></div>}
        {addFilesMode === 'document' && <div className="admin-add-form"><button type="button" className="admin-modal-back" onClick={() => setAddFilesMode('choose')}>← Back to options</button><label>Existing machine<div className="admin-machine-picker"><input type="search" value={machineSearch} onChange={event => setMachineSearch(event.target.value)} placeholder="Search machines..." aria-label="Search existing machines" />{selectedMachine && <span className="admin-selected-machine">Selected: {selectedMachine}</span>}<div className="admin-machine-results" role="listbox" aria-label="Existing machines">{visibleMachines.length ? visibleMachines.map(machine => <button type="button" role="option" aria-selected={selectedMachine === machine} key={machine} onClick={() => setSelectedMachine(machine)}>{machine}<span aria-hidden="true">→</span></button>) : <p>No matching machines</p>}</div></div></label><div className="admin-file-drop"><span aria-hidden="true">↑</span><strong>Choose a document</strong><small>PDF, DOCX, or TXT · UI preview only</small><input type="file" aria-label="Choose a document" onChange={event => setSelectedFile(event.target.files?.[0] || null)} />{selectedFile && <em>{selectedFile.name}</em>}</div><button type="button" className="admin-primary-action" disabled={!selectedMachine || !selectedFile} onClick={closeAddFiles}>Add document</button></div>}
      </section>
    </div>}
    <AdminFilesModal open={addFilesOpen} onClose={closeAddFiles} />
  </main>;
}
