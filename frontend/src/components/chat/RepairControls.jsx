import { useState } from 'react';
import { validateImage } from '../../utils/validateImage';

export default function RepairControls({ workflow, loading, onAction }) {
  const [finishing, setFinishing] = useState(false);
  const [reporting, setReporting] = useState(false);
  const [issueReport, setIssueReport] = useState('');
  const [issueImage, setIssueImage] = useState(null);
  const [issueImageError, setIssueImageError] = useState('');
  const [details, setDetails] = useState({
    machine: workflow.machine || '', issue: workflow.issue || '', cause: '',
    action_taken: (workflow.completed_steps || []).map(step => step.instruction).join('\n'),
  });
  const identity = { workflow_id: workflow.workflow_id, step_id: workflow.current_step?.step_id || '' };
  const update = event => setDetails(previous => ({ ...previous, [event.target.name]: event.target.value }));
  const chooseIssueImage = event => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    const validation = validateImage(file);
    if (!validation.valid) {
      setIssueImageError(validation.error);
      return;
    }
    setIssueImage({ file, previewUrl: URL.createObjectURL(file) });
    setIssueImageError('');
  };
  const removeIssueImage = () => {
    if (issueImage?.previewUrl) URL.revokeObjectURL(issueImage.previewUrl);
    setIssueImage(null);
    setIssueImageError('');
  };
  if (reporting) return <form className="repair-issue-form" onSubmit={event => {
    event.preventDefault();
    const report = issueReport.trim();
    if (!loading && report) onAction({ ...identity, action: 'report_issue', issue_report: report,
      imageFile: issueImage?.file, imagePreviewUrl: issueImage?.previewUrl });
  }}>
    <h3>Report what happened</h3>
    <p>The current step stays in this repair chat. Describe the result so the agent can reassess it.</p>
    <label>What happened?<textarea value={issueReport} onChange={event => setIssueReport(event.target.value)}
      maxLength={2000} minLength={1} required disabled={loading}
      placeholder="Example: Hydraulic pressure is still zero after checking the pump." /></label>
    <label>Attach an image (optional)
      <input type="file" accept="image/png,image/jpeg,image/webp" onChange={chooseIssueImage} disabled={loading} />
    </label>
    {issueImage && <div className="repair-issue-image-preview">
      <img src={issueImage.previewUrl} alt="Reported issue preview" />
      <button type="button" onClick={removeIssueImage} disabled={loading}>Remove image</button>
    </div>}
    {issueImageError && <p className="repair-image-error" role="alert">{issueImageError}</p>}
    <div className="repair-actions">
      <button type="submit" disabled={loading || !issueReport.trim()}>{loading ? 'Sending…' : 'Submit issue'}</button>
      <button type="button" disabled={loading} onClick={() => { removeIssueImage(); setReporting(false); setIssueReport(''); }}>Cancel</button>
    </div>
  </form>;
  if (finishing) return <form className="repair-report-form" onSubmit={event => {
    event.preventDefault();
    if (!loading) onAction({ ...identity, ...details, action: 'resolve' });
  }}>
    <h3>Confirm completed maintenance</h3>
    <p>Confirm what actually happened. Leave the cause blank if it was not confirmed.</p>
    <label>Machine<input name="machine" value={details.machine} onChange={update} maxLength={120} required disabled={loading} /></label>
    <label>Issue<input name="issue" value={details.issue} onChange={update} maxLength={1000} required disabled={loading} /></label>
    <label>Confirmed cause (optional)<input name="cause" value={details.cause} onChange={update} maxLength={1000} disabled={loading} /></label>
    <label>Action taken<textarea name="action_taken" value={details.action_taken} onChange={update} maxLength={4000} required disabled={loading} /></label>
    <p>Submitting confirms the issue is resolved. The report will be created automatically in this chat.</p>
    <div className="repair-actions">
      <button type="submit" disabled={loading}>Confirm resolved</button>
      <button type="button" disabled={loading} onClick={() => setFinishing(false)}>Cancel</button>
    </div>
  </form>;
  return <div className="repair-controls">
    {workflow.status === 'awaiting_done' && <>
      <p>Complete this step, then click Done for the next step. If the issue is fixed, choose Issue resolved.</p>
      <button type="button" disabled={loading} onClick={() => onAction({ ...identity, action: 'done' })}>
        {loading ? 'Please wait…' : 'Done'}
      </button>
    </>}
    {workflow.current_step && <button type="button" className="repair-report-button" disabled={loading}
      onClick={() => setReporting(true)}>Did not fix / Report issue</button>}
    <button type="button" disabled={loading} onClick={() => setFinishing(true)}>Issue resolved</button>
  </div>;
}

export function MaintenanceReport({ report }) {
  return <section className="maintenance-report" aria-label="Maintenance report">
    <h3>Maintenance report</h3>
    <dl>{[['Machine', report.machine], ['Issue', report.issue], ['Cause', report.cause],
      ['Action taken', report.action_taken], ['Status', report.status]].map(([label, value]) =>
      <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <p className="reference-notice">Technician-confirmed report · {new Date(report.created_at).toLocaleString()}</p>
  </section>;
}
