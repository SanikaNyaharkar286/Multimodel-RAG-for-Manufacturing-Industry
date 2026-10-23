import { useEffect, useState } from 'react';
import { documentRequest, downloadDocument } from '../../services/documentationApi';
import ResponseRenderer from './ResponseRenderer';
import useDialogFocus from '../../hooks/useDialogFocus';

const FIELDS = [['title','Report title'], ['machine','Machine'], ['model','Model'], ['asset_id','Asset ID'],
  ['issue','Original reported problem'], ['cause','Confirmed cause'], ['action_taken','Corrective action reported'],
  ['verification','Verification checks and results'], ['remaining_issues','Remaining issues'],
  ['escalation_notes','Escalation notes'], ['notes','Technician notes'], ['content','Additional documentation']];

export default function Documentation({ response }) {
  const [report, setReport] = useState(response.documentation || null);
  const [open, setOpen] = useState(Boolean(response.documentation));
  const [fields, setFields] = useState(response.documentation || {});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState('');
  const dialogRef = useDialogFocus(open && Boolean(report), () => setOpen(false));
  const dirty = report && FIELDS.some(([key]) => (fields[key] || '') !== (report[key] || ''));
  useEffect(() => {
    if (!response.documentation?.report_id) return undefined;
    let active = true;
    documentRequest(`/reports/${response.documentation.report_id}`).then(value => {
      if (active) { setReport(value); setFields(value); }
    }).catch(err => { if (active) setError(err.message); });
    return () => { active = false; };
  }, [response.documentation?.report_id]);
  useEffect(() => {
    if (!open) return undefined;
    const before = document.body.style.overflow; document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = before; };
  }, [open]);

  async function show() {
    setError(''); setBusy(true);
    try {
      const value = report ? await documentRequest(`/reports/${report.report_id}`)
        : await documentRequest(`/sessions/${response.session_id}/reports`, {
          case_id: response.repair_workflow?.workflow_id || '', message_id: response.message_id || '',
        });
      setReport(value); setFields(value); setOpen(true);
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  async function save() {
    setError(''); setBusy(true);
    try {
      const value = await documentRequest(`/reports/${report.report_id}`, {
        revision: report.revision, fields: Object.fromEntries(FIELDS.map(([key]) => [key, fields[key] || ''])),
      });
      setReport(value); setFields(value); setSaved('Changes saved. The original conversation is preserved.');
      return value;
    } catch (err) { setError(err.message); return null; } finally { setBusy(false); }
  }
  async function download() {
    const current = dirty ? await save() : report;
    if (!current) return;
    setBusy(true); setError('');
    try { await downloadDocument(current); } catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  function evidenceResponse(message) {
    return { ...message.response, blocks: (message.response?.blocks || []).map(block => block.type === 'image'
      ? { ...block, src: `/reports/${report.report_id}/images/${block.src.split('/').pop()}` } : block) };
  }
  return <div className="documentation-actions">
    <button type="button" onClick={show} disabled={busy}>{busy ? 'Opening documentation…' : report ? 'Open documentation' : 'Create PDF'}</button>
    {error && !open && <p role="alert">{error}</p>}
    {open && report && <div className="documentation-overlay"><section ref={dialogRef} tabIndex={-1} className="documentation-page" role="dialog" aria-modal="true" aria-label="Editable documentation">
      <header className="documentation-header"><div><span className="report-kicker">MAINTENANCE WORKSPACE</span><h2>Documentation preview</h2><p>{report.report_number} · {report.status}</p></div>
        <button type="button" onClick={() => setOpen(false)}>Back to Conversation</button></header>
      <div className="report-meta"><span>Conversation: {report.session_id}</span><span>Case: {report.case_id || 'Information answer'}</span>
        <span>Created: {new Date(report.created_at).toLocaleString('en-IN', { timeZone: report.timezone })} ({report.timezone})</span>
        <span>Technician: {report.technician_name || report.technician_email || 'Not recorded'}</span></div>
      <p className="report-ai-note">Suspected causes and AI observations are not confirmed findings. Record only work, measurements and verification that actually occurred.</p>
      <div className="documentation-fields">{FIELDS.map(([key,label]) => <label key={key}>{label}<textarea value={fields[key] || ''} placeholder="Not recorded" rows={key === 'content' || key === 'action_taken' ? 4 : 2}
        onChange={event => { setFields(previous => ({...previous, [key]:event.target.value})); setSaved(''); }} /></label>)}</div>
      {report.workflow?.safety && <section><h3>Safety instructions presented</h3><ul>{report.workflow.safety.bullets.map(bullet => <li key={bullet}>{bullet}</li>)}</ul>
        <p>Technician confirmations: {(report.workflow.safety_confirmations || []).length ? 'Recorded with the completed step(s) below.' : 'Not recorded'}</p></section>}
      <section><h3>Completed actions</h3>{report.workflow?.completed_steps?.length ? <ol>{report.workflow.completed_steps.map(step => <li key={step.step_id}>{step.instruction}{step.observation && <p>Observation: {step.observation}</p>}</li>)}</ol> : <p>Not recorded</p>}</section>
      <section><h3>Conversation evidence and manual references</h3>{report.history.map(message => <article className="documentation-evidence" key={message.id}>
        {message.role === 'user' ? <><strong>Technician question / observation</strong><p>{message.text}</p>{message.image && <img src={message.image} alt="Technician evidence" />}</>
          : <ResponseRenderer response={evidenceResponse(message)} documentationMode showSafetyWarning={false}/>}</article>)}</section>
      <footer className="documentation-footer">{error && <p role="alert">{error}</p>}{saved && <p role="status">{saved}</p>}
        <button type="button" disabled={busy || !dirty} onClick={save}>Save Changes</button>
        <button type="button" disabled={busy} onClick={download}>{busy ? 'Processing…' : 'Download PDF'}</button>
        <button type="button" onClick={() => setOpen(false)}>Back to Conversation</button></footer>
    </section></div>}
  </div>;
}
