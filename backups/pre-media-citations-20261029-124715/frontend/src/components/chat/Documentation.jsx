import { useEffect, useRef, useState } from 'react';
import { documentRequest, downloadDocument } from '../../services/documentationApi';
import useDialogFocus from '../../hooks/useDialogFocus';
import ReportEvidence from './ReportEvidence';
import ResponseRenderer from './ResponseRenderer';
import { REPORT_GROUPS, STEP_STATUSES, TEST_RESULTS, reviewFields, reviewSteps, reportEdit,
  hasReportEdits, validateReview, localDateTime, confirmedDateTime, issueDuration, updateStepConfirmation, UNPERFORMED_STATUSES } from './reportReview';
import '../../styles/report-review.css';
import { submitReport } from '../../services/reportSubmissionsApi';
import ReportPdf, { SubmissionStatus } from '../reports/ReportPdf';

function dateText(value, timezone) {
  if (!value || Number.isNaN(Date.parse(value))) return 'Not recorded';
  try { return new Date(value).toLocaleString(undefined, { timeZone: timezone || 'UTC', timeZoneName: 'short' }); }
  catch { return new Date(value).toISOString(); }
}

export function ReviewField({ definition: [key, label, type = 'text'], value, onChange, disabled = false }) {
  const calendarOnly = type === 'datetime-local' && /^\d{4}-\d{2}-\d{2}$/.test(value || '');
  const inputType = calendarOnly ? 'date' : type === 'date' && value?.includes('T') ? 'datetime-local' : type;
  const props = { name: key, value: value || '', disabled, onChange: event => onChange(key,
    inputType === 'datetime-local' ? confirmedDateTime(event.target.value) : event.target.value) };
  return <label className="report-review-field"><span>{label}</span>
    {Array.isArray(type) ? <select {...props}>
      {value && !type.includes(value) && <option value={value}>{value} (recorded)</option>}
      {type.map(option => <option key={option} value={option}>{option || 'Not recorded'}</option>)}
    </select> : type === 'textarea' ? <textarea {...props} rows={3} maxLength={key === 'parts_used' ? 4000 : 20000} placeholder="Not recorded" />
      : <input {...props} type={inputType} {...(inputType === 'datetime-local' ? { value: localDateTime(value), step: 1 } : {})} maxLength={20000} placeholder="Not recorded" />}
    {calendarOnly && <small>Calendar date only; time not recorded. Clear to enter a confirmed time.</small>}
  </label>;
}

function SourceList({ sources = [] }) {
  return sources.length > 0 && <ul className="report-step-sources">{sources.map((source, index) => <li key={index}>{typeof source === 'string' ? source
    : [source.document_name, source.page_number != null ? `PDF page ${source.page_number}` : ''].filter(Boolean).join(' · ')}</li>)}</ul>;
}

// Keep retrieved text, tables and reference links available. Images are shown
// through the report-owned authenticated evidence endpoints in their own group.
function ManualGuidance({ guidance }) {
  return <ResponseRenderer response={{ blocks: (guidance.blocks || []).filter(block => block.type !== 'image'), sources: guidance.sources || [] }} documentationMode showSafetyWarning={false} />;
}

export function StepReview({ steps, onChange }) {
  if (!steps.length) return <p>No structured troubleshooting steps recorded. Recommended actions are not confirmed work.</p>;
  return <div className="report-step-list">{steps.map((step, index) => <details className="report-step" key={step.step_id || `unidentified-${index}`}>
    <summary><span>Step {step.number ?? index + 1}: {step.title || step.instruction || 'Troubleshooting action'}</span><span className={`report-step-status report-step-status--${step.status.toLowerCase().replaceAll(' ', '-')}`}>{step.status} · {step.result}</span></summary>
    <div className="report-step-body"><p className="report-procedure-label">Recommended procedure from the recorded troubleshooting session</p>
      {step.instruction && <p className="report-preserve-lines">{step.instruction}</p>}
      {step.description && step.description !== step.instruction && <p className="report-preserve-lines">{step.description}</p>}
      {step.blocks?.length > 0 && <details><summary>Recorded manual details and reference tables</summary><ManualGuidance guidance={step} /></details>}
      {step.precautions?.length > 0 && <aside className="report-step-precautions"><strong>Safety precautions</strong><ul>{step.precautions.map((item, i) => <li key={i}>{item}</li>)}</ul></aside>}
      <SourceList sources={step.sources} />
      <p>Performed by: {step.performed_by || 'Not recorded'}</p>
      {step.evidence_ids?.length > 0 && <p>Evidence references: {step.evidence_ids.join(', ')}. Review the images in Supporting evidence.</p>}
      {!step.step_id && <p role="note">This step has no recorded identifier and cannot be edited.</p>}
      <fieldset disabled={!step.step_id}><legend>Technician confirmation for step {step.number ?? index + 1}</legend>
        <div className="report-review-fields">{[
          ['status', 'Step status', STEP_STATUSES], ['result', 'Actual test result', TEST_RESULTS],
          ['action_performed', 'Action actually performed', 'textarea'], ['observation', 'Technician observation', 'textarea'],
          ['parts_used', 'Confirmed parts used', 'textarea'], ['notes', 'Step notes', 'textarea'], ['completed_at', 'Actual completion time', 'datetime-local'],
        ].map(definition => <ReviewField key={definition[0]} definition={definition} value={step[definition[0]]}
          disabled={UNPERFORMED_STATUSES.includes(step.status) && ['action_performed', 'parts_used', 'completed_at', 'result'].includes(definition[0])}
          onChange={(key, value) => onChange(index, key, value)} />)}</div>
        {UNPERFORMED_STATUSES.includes(step.status) && <p className="report-review-hint">Select Completed or Failed to record performed work. Selecting Pending, Skipped or Not performed clears action, parts, result and completion time.</p>}
      </fieldset>
    </div>
  </details>)}</div>;
}

export default function Documentation({ response }) {
  const [report, setReport] = useState(response.documentation || null);
  const [open, setOpen] = useState(Boolean(response.documentation));
  const [fields, setFields] = useState(() => reviewFields(response.documentation || {}));
  const [steps, setSteps] = useState(() => reviewSteps(response.documentation || {}));
  const [busy, setBusy] = useState(Boolean(response.documentation?.report_id));
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [saved, setSaved] = useState('');
  const submitting = useRef(false);
  const [submissionBusy, setSubmissionBusy] = useState(false);
  const dialogRef = useDialogFocus(open && Boolean(report), () => { if (!busy) setOpen(false); });
  const edit = report ? reportEdit(report, fields, steps) : null;
  const dirty = edit && hasReportEdits(edit);
  const maintenance = Boolean(response.repair_workflow || report?.case_id);
  const guidance = report?.manual_guidance ?? (report?.history || []).flatMap(message => message.response ? [message.response] : []);

  function accept(value) {
    setReport(value); setFields(reviewFields(value)); setSteps(reviewSteps(value)); setConflict(false);
  }

  useEffect(() => {
    if (!response.documentation?.report_id) return undefined;
    let active = true;
    documentRequest(`/reports/${encodeURIComponent(response.documentation.report_id)}`).then(value => {
      if (active) { setReport(value); setFields(reviewFields(value)); setSteps(reviewSteps(value)); }
    }).catch(err => { if (active) setError(err.message); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [response.documentation?.report_id]);

  useEffect(() => {
    if (!open) return undefined;
    const before = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = before; };
  }, [open]);

  async function show() {
    // Closing a review retains its unsaved values in this mounted view.
    if (report && dirty) { setOpen(true); return; }
    setError(''); setSaved(''); setBusy(true);
    try {
      const value = report ? await documentRequest(`/reports/${encodeURIComponent(report.report_id)}`)
        : await documentRequest(`/sessions/${encodeURIComponent(response.session_id)}/reports`, {
          case_id: response.repair_workflow?.workflow_id || '', message_id: response.message_id || '',
        });
      accept(value); setOpen(true);
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  async function persist() {
    const validation = validateReview(fields, steps);
    if (validation) throw new Error(validation);
    if (!dirty) return report;
    const value = await documentRequest(`/reports/${encodeURIComponent(report.report_id)}`, edit);
    accept(value);
    return value;
  }

  async function save(download = false) {
    setError(''); setSaved(''); setBusy(true);
    try {
      const current = await persist();
      if (download) await downloadDocument(current);
      setSaved(download ? 'Report snapshot downloaded. Return to service remains not authorized.' : 'Draft saved. Pending steps remain as recorded.');
    } catch (err) { setError(err.message); setConflict(err.status === 409); } finally { setBusy(false); }
  }

  async function reload() {
    setBusy(true); setError(''); setSaved('');
    try { accept(await documentRequest(`/reports/${encodeURIComponent(report.report_id)}`)); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  async function sendToAdmin() {
    if (submitting.current || report.submission || busy) return;
    submitting.current = true; setSubmissionBusy(true); setBusy(true); setError(''); setSaved('');
    try {
      const current = await persist();
      const submission = await submitReport(current);
      accept({ ...current, submission });
      setSaved('Report submitted to admin successfully.');
    } catch (err) {
      // A lost response is not proof of failure. Reconcile the canonical
      // server state before offering an idempotent retry.
      try {
        const latest = await documentRequest(`/reports/${encodeURIComponent(report.report_id)}`);
        if (latest.submission) { accept(latest); setSaved('Report submitted to admin successfully.'); return; }
      } catch { /* Retain the local report and show the original error. */ }
      setError(`${err.message} Reopen the report to check its submission state before retrying.`);
      setConflict(err.status === 409);
    } finally { submitting.current = false; setSubmissionBusy(false); setBusy(false); }
  }

  function update(key, value) { setFields(previous => ({ ...previous, [key]: value })); setSaved(''); }
  function updateStep(index, key, value) {
    setSteps(previous => previous.map((step, i) => i === index ? updateStepConfirmation(step, key, value) : step)); setSaved('');
  }

  return <div className="documentation-actions">
    <button type="button" onClick={show} disabled={busy}>{busy ? 'Opening report…' : maintenance ? (report ? 'Review Maintenance Report' : 'Generate Maintenance Report') : report ? 'Open documentation' : 'Create PDF'}</button>
    {!maintenance && <button type="button" onClick={show} disabled={busy}>Download PDF</button>}
    {error && !open && <p role="alert">{error}</p>}
    {open && report && <div className="documentation-overlay report-review-overlay"><section ref={dialogRef} tabIndex={-1} className="documentation-page report-review-page" role="dialog" aria-modal="true" aria-label="Editable documentation" aria-busy={busy}>
      <header className="documentation-header"><div><span className="report-kicker">MANUFACTURING MAINTENANCE COPILOT</span><h2>Technical Service &amp; Maintenance Report</h2><p>Review the report snapshot before saving or downloading.</p><p>{report.report_number || report.report_id} · {fields.status || 'Not recorded'}</p></div>
        <button type="button" disabled={busy} onClick={() => setOpen(false)}>Back to Conversation</button></header>
      <div className="report-meta"><span>Report ID: {report.report_id}</span><span>Incident / case: {report.case_id || 'Not recorded'}</span>
        <span>Generated: {dateText(report.created_at, report.timezone)}</span><span>Snapshot updated: {dateText(report.updated_at || report.created_at, report.timezone)}</span>
        <span>Technician: {report.technician_name || 'Not recorded'}</span><span>Technician email: {report.technician_email || 'Not recorded'}</span>
        <span>Employee / service ID: {report.employee_id || 'Not recorded'}</span><span>Assigned date: {report.assigned_date || 'Not recorded'}</span></div>
      <p className="report-ai-note">Record only confirmed work, observations, measurements and parts. Recommended procedures remain pending until explicitly confirmed. An ongoing or unresolved investigation can be saved and downloaded.</p>
      <p className="report-review-authorization"><strong>Return-to-service authorization: Not authorized.</strong> This application has no return-to-service approval or signature workflow. A resolved issue or a downloaded report does not authorize operation. Signature Pending.</p>
      <SubmissionStatus submission={report.submission} />
      {report.submission && <p>This submitted snapshot is read-only. Administrator review does not authorize return to service.</p>}
      <form noValidate onSubmit={event => { event.preventDefault(); save(); }}>
        <fieldset className="report-review-form" disabled={busy || conflict || Boolean(report.submission)}><legend className="report-sr-only">Maintenance report review</legend>
          {REPORT_GROUPS.map((group, index) => <details className="report-review-group" key={group.title} open={index === 0 ? true : undefined}>
            <summary>{index + 1}. {group.title}</summary>
            <div className="report-review-group-body">
              {index === 1 && <p>Technician name and email above are read-only and supplied by the authenticated report service.</p>}
              {index === 2 && <><p>Issue duration: {issueDuration(fields.fault_started_at, report.updated_at || report.created_at)}</p><p>Enter actual fault dates; the conversation date is not a fault start date.</p></>}
              {index === 3 && !fields.action_taken && !fields.components_repaired && !fields.components_replaced && <p>No confirmed repair recorded.</p>}
              {index === 4 && !fields.cause && <p>Root cause not confirmed. Further investigation required.</p>}
              <div className="report-review-fields">{group.fields.map(definition => <ReviewField key={definition[0]} definition={definition} value={fields[definition[0]]}
                disabled={definition[0] === 'machine' && Boolean(report.source_machine)} onChange={update} />)}</div>
              {index === 0 && report.source_machine && <p className="report-review-hint">Machine name is bound to this incident and its manual evidence. Record the plant identifier separately in Machine / asset ID.</p>}
              {group.fields.some(([, , type]) => type === 'datetime-local') && <p className="report-review-hint">Date and time inputs use your device timezone ({Intl.DateTimeFormat().resolvedOptions().timeZone}); saved timestamps include a timezone. Leave unknown dates blank.</p>}
            </div>
          </details>)}
          <details className="report-review-group" open><summary>8. Troubleshooting log · {steps.filter(step => step.status === 'Completed').length} completed / {steps.length} recorded</summary>
            <div className="report-review-group-body"><p>Review each step separately. Changing a status does not fill in an action, test result or completion time.</p>
              {report.workflow?.safety?.bullets?.length > 0 && <aside className="report-step-precautions"><strong>Safety instructions presented</strong><ul>{report.workflow.safety.bullets.map((bullet, i) => <li key={i}>{bullet}</li>)}</ul><p>Recorded safety confirmations: {(report.workflow.safety_confirmations || []).length}</p></aside>}
              <StepReview steps={steps} onChange={updateStep} /></div>
          </details>
        </fieldset>
        <details className="report-review-group"><summary>9. Supporting evidence · {(report.evidence || []).length} reference(s)</summary><div className="report-review-group-body"><ReportEvidence report={report} /></div></details>
        {guidance.length > 0 && <details className="report-review-group"><summary>10. Recorded manual guidance</summary><div className="report-review-group-body"><p>Retrieved guidance and possible causes are recommendations. Technician-confirmed findings and work are recorded separately above.</p>{guidance.map((item, index) => <article className="documentation-evidence" key={index}>{item.step_id && <p>Step reference: {item.step_id}</p>}<ManualGuidance guidance={item} /></article>)}</div></details>}
        <footer className="documentation-footer">
          {error && <p role="alert">{error}</p>}{saved && <p role="status">{saved}</p>}
          {dirty && <p className="report-review-hint">Unsaved changes. Save this draft before leaving the conversation.</p>}
          {conflict && <><p>Your entries are retained above. Reopening the latest revision replaces these unsaved entries.</p><button type="button" disabled={busy} onClick={reload}>Reopen latest revision (discard edits)</button></>}
          <button type="submit" disabled={busy || !dirty || conflict || Boolean(report.submission)}>Save draft</button>
          <button type="button" disabled={busy || conflict} onClick={() => save(true)}>Download PDF</button>
          <ReportPdf report={report} disabled={busy || Boolean(dirty)} hideDownload />
          <button type="button" disabled={busy || conflict || Boolean(report.submission)} onClick={sendToAdmin}>{report.submission ? 'Submitted to Admin' : submissionBusy ? 'Submitting…' : 'Send to Admin'}</button>
          {!report.submission && <p>Sending saves your reviewed changes and submits an immutable PDF. Email delivery is tracked separately.</p>}
          {busy && <p role="status">Processing report…</p>}
          <button type="button" disabled={busy} onClick={() => setOpen(false)}>Back to Conversation</button>
        </footer>
      </form>
    </section></div>}
  </div>;
}
