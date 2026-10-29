import { useCallback, useEffect, useRef, useState } from 'react';
import { myReports } from '../../services/reportSubmissionsApi';
import ReportPdf from './ReportPdf';
import { reportDate, reviewLabel, emailLabel } from './reportDisplay';

export default function MySubmittedReports({ onCurrentReport, onBack }) {
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);
  const requestVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setBusy(true);
    try { const value = await myReports(page); if (version === requestVersion.current) { setData(value); setError(''); } }
    catch (err) { if (version === requestVersion.current) setError(err.message); }
    finally { if (version === requestVersion.current) setBusy(false); }
  }, [page]);
  useEffect(() => { load(); const timer = setInterval(load, 20000); return () => { clearInterval(timer); requestVersion.current++; }; }, [load]);
  return <main className="submitted-reports-page">
    <header><div><p className="admin-eyebrow">MAINTENANCE RECORDS</p><h1>My Submitted Reports</h1><p>Private report submissions and the latest administrator review status.</p></div>
      <div className="report-pdf-actions"><button onClick={onBack}>Back to chat</button><button onClick={onCurrentReport}>Current conversation report</button><button disabled={busy} onClick={load}>Refresh</button></div></header>
    {error && <p role="alert">{error}</p>}{busy && !data && <p role="status">Loading submitted reports…</p>}
    {data && <><p>{data.total} submitted report(s)</p><div className="reports-table-scroll"><table className="reports-table"><thead><tr>
      {['Report ID', 'Machine / model', 'Issue', 'Submitted', 'Review status', 'Email status', 'Actions'].map(label => <th key={label}>{label}</th>)}
    </tr></thead><tbody>{data.reports.map(report => <tr key={report.report_id}>
      <td>{report.report_number || report.report_id}</td><td>{report.machine_name || 'Not recorded'}<small>{report.machine_model}</small></td>
      <td>{report.issue_description || 'Not recorded'}</td><td>{reportDate(report.submitted_at)}</td>
      <td>{reviewLabel(report.review_status)}{report.admin_comment && <p>Admin comment: {report.admin_comment}</p>}</td>
      <td>{emailLabel(report)}</td><td><ReportPdf report={report} /></td>
    </tr>)}</tbody></table></div>{!data.reports.length && <p>No submitted reports yet. Generate and review a report in chat, then select Send to Admin.</p>}
      <div className="reports-pagination"><button disabled={page === 1 || busy} onClick={() => setPage(page-1)}>Previous</button><span>Page {page}</span><button disabled={page*20 >= data.total || busy || page >= 1000} onClick={() => setPage(page+1)}>Next</button></div></>}
  </main>;
}
