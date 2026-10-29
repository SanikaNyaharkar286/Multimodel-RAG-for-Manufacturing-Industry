import { useCallback, useEffect, useRef, useState } from 'react';
import { adminReports, adminReport, reviewReport } from '../../services/reportSubmissionsApi';
import ReportPdf, { SubmissionStatus } from './ReportPdf';
import { reportDate, reviewLabel } from './reportDisplay';
import ResponseRenderer from '../chat/ResponseRenderer';

export function AdminReportDetail({ reportId, onBack, onUpdated }) {
  const [detail, setDetail] = useState(null);
  const [status, setStatus] = useState('new');
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const [conflict, setConflict] = useState(false);
  const requestVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setBusy(true); setError('');
    try { const value = await adminReport(reportId); if (version === requestVersion.current) { setDetail(value); setStatus(value.submission.review_status); setComment(value.submission.admin_comment || ''); setConflict(false); } }
    catch (err) { if (version === requestVersion.current) setError(err.status === 404 ? 'This report is no longer available. Return to the report list.' : err.message); }
    finally { if (version === requestVersion.current) setBusy(false); }
  }, [reportId]);
  useEffect(() => { load(); return () => { requestVersion.current++; }; }, [load]);
  async function save(event) {
    event.preventDefault(); if (busy || !detail) return;
    setBusy(true); setError(''); setSaved('');
    try {
      const submission = await reviewReport(reportId, { review_status: status, admin_comment: comment, version: detail.submission.version });
      setDetail(previous => ({ ...previous, submission })); setSaved('Review saved. The submitted PDF is unchanged.'); onUpdated?.();
    } catch (err) { setError(err.message); setConflict(err.status === 409); }
    finally { setBusy(false); }
  }
  return <section className="admin-report-detail" aria-label="Submitted maintenance report">
    <button type="button" onClick={onBack}>Back to Maintenance Reports</button>
    {error && <p role="alert">{error}</p>}{busy && !detail && <p role="status">Loading submitted report…</p>}
    {!detail && !busy && <button onClick={load}>Retry</button>}
    {detail && <><h1>{detail.submission.report_number}</h1><p>Submitted {reportDate(detail.submission.submitted_at)}</p>
      <div className="admin-report-facts"><section><h2>Technician information</h2><p>{detail.submission.technician_name || 'Not recorded'}</p><p>{detail.submission.technician_email}</p></section>
        <section><h2>Equipment information</h2><p>{detail.submission.machine_name || 'Not recorded'}</p><p>Model: {detail.submission.machine_model || 'Not recorded'}</p></section></div>
      <SubmissionStatus submission={detail.submission} /><ReportPdf key={reportId} report={detail.submission} admin disabled={busy} />
      <section><h2>Troubleshooting information</h2>{[
        ['Reported issue', 'issue'], ['Findings / cause', 'cause'], ['Confirmed corrective actions', 'action_taken'],
        ['Verification', 'verification'], ['Remaining work', 'remaining_work'], ['Final recommendations', 'preventive_recommendations'],
      ].map(([label, key]) => <div className="admin-report-field" key={key}><h3>{label}</h3><p>{detail.report[key] || 'Not recorded'}</p></div>)}
        <details><summary>Safety guidance</summary><ul>{(detail.report.workflow?.safety?.bullets || []).map((bullet, i) => <li key={i}>{bullet}</li>)}</ul>{!detail.report.workflow?.safety?.bullets?.length && <p>Not recorded</p>}</details>
        <details><summary>Recorded troubleshooting steps · {(detail.report.steps || []).length}</summary>{(detail.report.steps || []).map((step, index) => <article className="admin-recorded-step" key={step.step_id || index}>
          <h3>Step {step.number || index + 1}: {step.title || step.instruction}</h3><p>{step.instruction}</p><p>{step.description}</p><p>Status: {step.status} · Result: {step.result}</p>
          <p>Action performed: {step.action_performed || 'Not recorded'}</p><p>Observation: {step.observation || 'Not recorded'}</p><p>Parts used: {step.parts_used || 'Not recorded'}</p>
          <p>Notes: {step.notes || 'Not recorded'}</p><p>Completed: {reportDate(step.completed_at)}</p>
          {(step.precautions || []).map((text, i) => <p key={i}>Safety: {text}</p>)}
        </article>)}</details>
        <details><summary>Recorded manual guidance</summary>{(detail.report.manual_guidance || []).map((guidance, index) => <ResponseRenderer key={index}
          response={{ blocks: (guidance.blocks || []).filter(block => block.type !== 'image'), sources: guidance.sources || [] }} documentationMode showSafetyWarning={false} />)}</details>
        <p>The PDF contains the original layout and supporting images. Missing information is not inferred.</p>
      </section>
      <form className="admin-review-form" onSubmit={save}><h2>Administrative review</h2>
        <p>Review records do not change technical findings or authorize return to service.</p>
        <label>Review status<select value={status} disabled={busy || conflict} onChange={event => setStatus(event.target.value)}>
          {['new', 'under_review', 'reviewed'].map(value => <option value={value} key={value}>{reviewLabel(value)}</option>)}</select></label>
        <label>Admin comments<textarea maxLength={10000} rows={5} value={comment} disabled={busy || conflict} onChange={event => setComment(event.target.value)} /></label>
        {detail.submission.assigned_admin_uid && <p>Last reviewed by: {detail.submission.assigned_admin_uid} · {reportDate(detail.submission.reviewed_at || detail.submission.updated_at)}</p>}
        {conflict && <><p>Your comment is retained. Reloading replaces it with the latest saved review.</p><button type="button" onClick={load}>Reload latest review (discard edits)</button></>}
        <button disabled={busy || conflict} type="submit">{busy ? 'Saving…' : 'Save review'}</button>{saved && <p role="status">{saved}</p>}
      </form>
    </>}
  </section>;
}

export default function AdminReports({ selectedId, onSelect, dashboard = false, onOpenReports }) {
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState('');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('desc');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);
  const requestVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setBusy(true);
    try { const value = await adminReports({ page, status, search: query, sort }); if (version === requestVersion.current) { setData(value); setError(''); } }
    catch (err) { if (version === requestVersion.current) setError(err.message); }
    finally { if (version === requestVersion.current) setBusy(false); }
  }, [page, status, query, sort]);
  useEffect(() => { load(); const timer = setInterval(load, 20000); return () => { clearInterval(timer); requestVersion.current++; }; }, [load]);
  if (selectedId) return <AdminReportDetail key={selectedId} reportId={selectedId} onBack={() => onSelect('')} onUpdated={load} />;
  return <section className="admin-reports" aria-label="Maintenance Reports"><h1>{dashboard ? 'Dashboard' : 'Maintenance Reports'}</h1>
    <p>Submitted reports from the maintenance workspace.</p>
    <div className="report-counts">{[['Total reports', data ? Object.values(data.counts).reduce((a,b) => a+b, 0) : '—'], ['New', data?.counts?.new ?? '—'], ['Under Review', data?.counts?.under_review ?? '—'], ['Reviewed', data?.counts?.reviewed ?? '—']].map(([label, value]) =>
      <div key={label}><strong>{value}</strong><span>{label}</span></div>)}</div>
    {dashboard ? <button onClick={onOpenReports}>Open Maintenance Reports</button> : <form className="admin-toolbar" onSubmit={event => { event.preventDefault(); setPage(1); setQuery(search.trim()); }}>
      <input type="search" aria-label="Search maintenance reports" maxLength={80} placeholder="Report ID, technician, machine, or issue" value={search} onChange={event => setSearch(event.target.value)} />
      <button type="submit">Search</button>
      <label>Status<select value={status} onChange={event => { setPage(1); setStatus(event.target.value); }}><option value="">All statuses</option>{['new','under_review','reviewed'].map(value => <option key={value} value={value}>{reviewLabel(value)}</option>)}</select></label>
      <label>Submitted<select value={sort} onChange={event => { setPage(1); setSort(event.target.value); }}><option value="desc">Newest first</option><option value="asc">Oldest first</option></select></label>
      <button type="button" onClick={load} disabled={busy}>Refresh</button>
    </form>}
    {error && <p role="alert">{error}</p>}{busy && !data && <p role="status">Loading reports…</p>}
    {!dashboard && data && <><p>{data.total} matching report(s)</p><div className="reports-table-scroll"><table className="reports-table"><thead><tr>
      {['Report ID', 'Technician', 'Machine', 'Issue', 'Submitted', 'Status', 'Action'].map(label => <th key={label}>{label}</th>)}
    </tr></thead><tbody>{data.reports.map(report => <tr key={report.report_id}>
      <td>{report.report_number}</td><td>{report.technician_name || report.technician_email}<small>{report.technician_email}</small></td><td>{report.machine_name || 'Not recorded'}</td>
      <td>{report.issue_description || 'Not recorded'}</td><td>{reportDate(report.submitted_at)}</td><td>{reviewLabel(report.review_status)}</td><td><button onClick={() => onSelect(report.report_id)}>View</button></td>
    </tr>)}</tbody></table></div>{!data.reports.length && <p>No matching submissions.</p>}
      <div className="reports-pagination"><button disabled={page === 1 || busy} onClick={() => setPage(page-1)}>Previous</button><span>Page {page}</span><button disabled={page*20 >= data.total || busy || page >= 1000} onClick={() => setPage(page+1)}>Next</button></div></>}
  </section>;
}
