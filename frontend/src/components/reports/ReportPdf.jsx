import { useEffect, useRef, useState } from 'react';
import { documentRequest, reportPdfUrl } from '../../services/documentationApi';
import useDialogFocus from '../../hooks/useDialogFocus';
import '../../styles/submitted-reports.css';
import { reportDate, reviewLabel, emailLabel } from './reportDisplay';

export function SubmissionStatus({ submission }) {
  if (!submission) return null;
  return <div className="submission-status" role="status"><strong>Submitted to Admin</strong>
    <p>Submitted: {reportDate(submission.submitted_at)} · Review: {reviewLabel(submission.review_status)}</p>
    <p>{emailLabel(submission)}</p>{submission.email_error && <p>{submission.email_error}</p>}
    {submission.admin_comment && <p>Admin comment: {submission.admin_comment}</p>}
  </div>;
}

export default function ReportPdf({ report, admin = false, disabled = false, hideDownload = false }) {
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const active = useRef(true);
  const objectUrl = useRef('');
  const focusRef = useDialogFocus(Boolean(url), () => setUrl(''));
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; if (objectUrl.current) URL.revokeObjectURL(objectUrl.current); };
  }, [report.report_id, admin]);
  useEffect(() => {
    if (!url && objectUrl.current) { URL.revokeObjectURL(objectUrl.current); objectUrl.current = ''; }
  }, [url]);
  async function open(download) {
    if (busy) return;
    setBusy(true); setError('');
    try {
      if (download) {
        const blob = await documentRequest(`${admin ? '/admin' : ''}/reports/${encodeURIComponent(report.report_id)}/pdf`, null, true);
        if (!active.current) return;
        const downloadUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = downloadUrl; link.download = `${report.report_number || report.report_id}.pdf`; link.click();
        setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
      } else {
        const preview = await reportPdfUrl(report.report_id, admin);
        if (!active.current) { URL.revokeObjectURL(preview); return; }
        if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
        objectUrl.current = preview; setUrl(preview);
      }
    } catch (err) { if (active.current) setError(err.message); }
    finally { if (active.current) setBusy(false); }
  }
  return <div className="report-pdf-actions">
    <button type="button" disabled={busy || disabled} onClick={() => open(false)}>View Report</button>
    {!hideDownload && <button type="button" disabled={busy || disabled} onClick={() => open(true)}>Download Report</button>}
    {busy && <span role="status">Loading PDF…</span>}{error && <p role="alert">{error}</p>}
    {url && <div className="pdf-preview-overlay"><section ref={focusRef} tabIndex={-1} className="pdf-preview-dialog" role="dialog" aria-modal="true" aria-label="Report PDF preview">
      <header><h2>Report PDF preview</h2><button type="button" onClick={() => setUrl('')}>Close preview</button></header>
      <iframe title="Maintenance report PDF" src={url} />
      <p>If your browser cannot preview PDFs, use Download Report.</p>
    </section></div>}
  </div>;
}
