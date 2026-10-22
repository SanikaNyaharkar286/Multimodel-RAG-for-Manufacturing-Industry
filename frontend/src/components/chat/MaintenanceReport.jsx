import { useState } from 'react';

export default function MaintenanceReport({ report }) {
  const [editing, setEditing] = useState(false);
  const [details, setDetails] = useState({
    machine: report.machine || '', issue: report.issue || '', cause: report.cause || '',
    action_taken: report.action_taken || '',
  });
  const update = event => setDetails(previous => ({ ...previous, [event.target.name]: event.target.value }));
  const reviewed = { ...report, ...details };
  const download = () => {
    const steps = (report.completed_steps || []).map((step, index) => `${index + 1}. ${step.instruction}`).join('\n');
    const content = [
      'Manufacturing Equipment Copilot - Maintenance Report', '',
      `Machine: ${reviewed.machine}`, `Issue: ${reviewed.issue}`, `Cause: ${reviewed.cause}`,
      `Action taken: ${reviewed.action_taken}`, `Status: ${reviewed.status}`,
      `Created: ${new Date(reviewed.created_at).toLocaleString()}`, '',
      'Completed troubleshooting steps:', steps || 'No completed steps recorded.', '',
      'Reported results:',
      (report.reported_issues || []).map(item => `Step ${item.step_number}: ${item.report}`).join('\n') || 'No failed steps reported.',
    ].join('\n');
    const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `maintenance-report-${reviewed.machine || 'equipment'}.txt`;
    link.click();
    URL.revokeObjectURL(url);
  };
  return <section className="maintenance-report" aria-label="Maintenance report">
    <div className="maintenance-report-heading"><h3>Maintenance report</h3>
      <div className="maintenance-report-actions">
        <button type="button" onClick={() => setEditing(previous => !previous)}>{editing ? 'Review report' : 'Edit report'}</button>
        <button type="button" onClick={download}>Download report</button>
      </div>
    </div>
    <p>Review the information before downloading the final report.</p>
    {editing ? <div className="maintenance-report-editor">
      <label>Machine<input name="machine" value={details.machine} onChange={update} maxLength={120} /></label>
      <label>Issue<input name="issue" value={details.issue} onChange={update} maxLength={1000} /></label>
      <label>Confirmed cause<textarea name="cause" value={details.cause} onChange={update} maxLength={1000} /></label>
      <label>Action taken<textarea name="action_taken" value={details.action_taken} onChange={update} maxLength={4000} /></label>
    </div> : <dl>{[['Machine', reviewed.machine], ['Issue', reviewed.issue], ['Cause', reviewed.cause], ['Action taken', reviewed.action_taken], ['Status', reviewed.status]].map(([label, value]) =>
      <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
    <h4>Completed troubleshooting steps</h4>
    {(report.completed_steps || []).length ? <ol>{report.completed_steps.map(step => <li key={step.step_id}>{step.instruction}</li>)}</ol> : <p>No completed steps recorded.</p>}
    {(report.reported_issues || []).length > 0 && <><h4>Reported results during troubleshooting</h4>
      <ul>{report.reported_issues.map((item, index) => <li key={`${item.step_id || index}-${index}`}>
        <strong>Step {item.step_number || index + 1}:</strong> {item.report}
      </li>)}</ul></>}
    <p className="reference-notice">Technician-confirmed report · {new Date(report.created_at).toLocaleString()}</p>
  </section>;
}
