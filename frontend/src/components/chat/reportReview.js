export const RESOLUTION_STATUSES = ['Resolved', 'Unresolved', 'Partially Resolved', 'Under Investigation', 'Awaiting Spare Parts', 'Awaiting OEM Support'];
export const STEP_STATUSES = ['Completed', 'Pending', 'Skipped', 'Failed', 'Not performed'];
export const TEST_RESULTS = ['Passed', 'Failed', 'Inconclusive', 'Not tested'];
const testOptions = ['', 'Pass', 'Fail', 'Not tested'];
export const UNPERFORMED_STATUSES = ['Pending', 'Skipped', 'Not performed'];

// Only report fields in the edit contract belong here. Identity and approval
// are supplied by the server, never by an editable client field.
export const REPORT_GROUPS = [
  { title: 'Machine identification', fields: [
    ['machine', 'Machine name'], ['model', 'Model number'], ['asset_id', 'Machine / asset ID'],
    ['machine_type', 'Machine type'], ['serial_number', 'Serial number'], ['installation_date', 'Installation date', 'date'],
    ['machine_location', 'Machine location'], ['operating_hours', 'Operating hours'], ['manufacturer', 'Equipment manufacturer'], ['warranty_status', 'Warranty / AMC status'],
  ] },
  { title: 'Technician and service', fields: [
    ['designation', 'Designation'], ['contact_number', 'Contact number'], ['service_company', 'Service company'], ['supervisor_name', 'Supervisor name'],
    ['service_start', 'Service start', 'datetime-local'], ['service_end', 'Service end', 'datetime-local'],
  ] },
  { title: 'Fault information', fields: [
    ['title', 'Report title'], ['issue', 'Original reported problem', 'textarea'],
    ['fault_started_at', 'Fault first occurred', 'datetime-local'], ['fault_reported_at', 'Fault reported', 'datetime-local'],
    ['fault_occurrence', 'When the fault occurs'], ['fault_frequency', 'Frequency of occurrence'], ['machine_state', 'Machine state when fault occurred'],
    ['severity', 'Assessed severity'], ['production_impact', 'Production impact', 'textarea'], ['downtime', 'Actual machine downtime'], ['reported_by', 'Reported by'],
  ] },
  { title: 'Repair and spare parts', fields: [
    ['action_taken', 'Corrective action actually performed', 'textarea'], ['repair_status', 'Repair status'],
    ['components_inspected', 'Components inspected', 'textarea'], ['components_repaired', 'Components repaired', 'textarea'], ['components_replaced', 'Components replaced', 'textarea'],
    ['part_numbers', 'Verified part numbers'], ['parts_quantity', 'Confirmed quantities'], ['replacement_status', 'Replacement status', ['', 'Installed', 'Required', 'Ordered', 'Pending', 'Not performed']],
    ['repair_date', 'Repair date', 'date'], ['post_repair_test', 'Post-repair test outcome', 'textarea'],
  ] },
  { title: 'Root cause and verification', fields: [
    ['cause', 'Confirmed root cause', 'textarea'], ['verification', 'Diagnostic findings and recorded measurements', 'textarea'],
    ['functional_test', 'Functional test', testOptions], ['safety_test', 'Safety test', testOptions],
    ['performance_observations', 'Actual performance observations', 'textarea'], ['technician_verification', 'Technician verification', ['', 'Confirmed', 'Pending']],
    ['preventive_recommendations', 'Technician-entered preventive recommendations', 'textarea'],
  ] },
  { title: 'Final status and remaining work', fields: [
    ['status', 'Issue resolution status', ['', ...RESOLUTION_STATUSES, 'Information']],
    ['operational_status', 'Machine operational status', ['', 'Operational', 'Not Operational', 'Restricted Operation', 'Under Maintenance', 'Awaiting Verification', 'Not Recorded']],
    ['remaining_issues', 'Remaining symptoms / concerns', 'textarea'], ['remaining_work', 'Remaining work', 'textarea'], ['next_action', 'Next action', 'textarea'],
    ['responsible_person', 'Responsible person'], ['expected_resolution_at', 'Expected resolution', 'datetime-local'],
  ] },
  { title: 'OEM escalation and technician remarks', fields: [
    ['escalation_required', 'Escalation required', ['', 'Yes', 'No']], ['assistance_requested', 'Technical assistance requested', 'textarea'],
    ['escalation_status', 'Recorded escalation status', ['', 'Not submitted', 'Required', 'Draft', 'Pending']], ['escalation_date', 'Escalation date', 'date'], ['follow_up_owner', 'Follow-up owner'],
    ['escalation_notes', 'Escalation notes', 'textarea'], ['notes', 'Final technician remarks', 'textarea'], ['content', 'Additional documentation', 'textarea'],
  ] },
];

const FIELD_DEFINITIONS = REPORT_GROUPS.flatMap(group => group.fields);
const CONFIRMATION_KEYS = ['step_id', 'status', 'observation', 'result', 'notes', 'parts_used', 'action_performed', 'completed_at'];
const text = value => value == null ? '' : String(value);

export function reviewFields(report) {
  return Object.fromEntries(FIELD_DEFINITIONS.map(([key]) => [key, text(report[key])]));
}

export function reviewSteps(report) {
  // Use the normalized server records. A recommended procedure is never proof
  // of completion, and list position must never become a synthetic step ID.
  return (report.steps || []).map(step => ({ ...step,
    status: STEP_STATUSES.includes(step.status) ? step.status : 'Pending',
    result: TEST_RESULTS.includes(step.result) ? step.result : 'Not tested',
    ...Object.fromEntries(['observation', 'notes', 'parts_used', 'action_performed', 'completed_at'].map(key => [key, text(step[key])])),
  }));
}

export function updateStepConfirmation(step, key, value) {
  const updated = { ...step, [key]: value };
  if (key === 'status' && UNPERFORMED_STATUSES.includes(value)) {
    // A deliberate status change retracts performed-work assertions. Keep
    // observations and notes, which may explain why the action was not done.
    Object.assign(updated, { action_performed: '', parts_used: '', completed_at: '', result: 'Not tested', performed_by: '' });
  }
  return updated;
}

export function reportEdit(report, fields, steps) {
  const original = reviewFields(report);
  const changed = Object.fromEntries(Object.entries(original).filter(([key, value]) => fields[key] !== value).map(([key]) => [key, fields[key]]));
  const baseline = reviewSteps(report);
  const confirmations = steps.filter((step, index) => step.step_id && CONFIRMATION_KEYS.some(key => step[key] !== baseline[index]?.[key]))
    .map(step => Object.fromEntries(CONFIRMATION_KEYS.map(key => [key, step[key] ?? ''])));
  return { revision: report.revision, fields: changed, ...(confirmations.length ? { step_confirmations: confirmations } : {}) };
}

export const hasReportEdits = edit => Boolean(Object.keys(edit.fields).length || edit.step_confirmations?.length);
const zonedDate = value => /^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/i.test(value) && !Number.isNaN(Date.parse(value));
const calendarDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
const contractInstant = value => Date.parse(calendarDate(value) ? `${value}T00:00:00+05:30` : value);

export function validateReview(fields, steps, now = Date.now()) {
  for (const [key, label, type] of FIELD_DEFINITIONS) {
    const value = fields[key];
    if (value?.length > 20000) return `${label} must be 20,000 characters or fewer.`;
    if (value && type === 'datetime-local' && !zonedDate(value) && !calendarDate(value)) return `${label} needs a valid date, time and timezone.`;
    if (value && type === 'date' && !calendarDate(value) && !zonedDate(value)) return `${label} needs a valid date.`;
    // Calendar dates in the report contract use the report service's India zone.
    const instant = contractInstant(value);
    if (value && ['date', 'datetime-local'].includes(type) && key !== 'expected_resolution_at' && instant > now) return `${label} cannot be in the future.`;
    if (value && Array.isArray(type) && !type.includes(value)) return `Select a valid ${label.toLowerCase()}.`;
  }
  if (fields.service_start && fields.service_end && contractInstant(fields.service_end) < contractInstant(fields.service_start)) return 'Service end cannot precede service start.';
  if (fields.fault_started_at && fields.fault_reported_at && contractInstant(fields.fault_reported_at) < contractInstant(fields.fault_started_at)) return 'Fault reported cannot precede the fault start.';
  for (const step of steps) {
    if (!STEP_STATUSES.includes(step.status) || !TEST_RESULTS.includes(step.result)) return 'Select a valid step status and test result.';
    if (step.completed_at && !zonedDate(step.completed_at) && !calendarDate(step.completed_at)) return 'Step completion needs a valid date or a time with timezone.';
    if (step.completed_at && contractInstant(step.completed_at) > now) return 'Step completion time cannot be in the future.';
    if (UNPERFORMED_STATUSES.includes(step.status) && (step.completed_at || step.action_performed || step.parts_used || step.result !== 'Not tested')) return 'Pending, skipped and not performed steps cannot contain performed actions, parts, completion times or test results.';
    if (step.parts_used.length > 4000) return 'Step parts used must be 4,000 characters or fewer.';
    if (CONFIRMATION_KEYS.some(key => typeof step[key] === 'string' && step[key].length > 20000)) return 'Step details must be 20,000 characters or fewer.';
  }
  return '';
}

export function localDateTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 19);
}

export function confirmedDateTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toISOString();
}

export function issueDuration(start, reportDate) {
  if (!start || !zonedDate(reportDate || '')) return 'Not reported';
  if (!zonedDate(start) && !calendarDate(start)) return 'Not reported';
  const elapsed = Date.parse(reportDate) - contractInstant(start);
  if (elapsed < 0) return 'Fault start is after this report snapshot; duration unavailable';
  const day = value => {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Calcutta', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(value));
    const fields = Object.fromEntries(parts.map(part => [part.type, part.value]));
    return Date.UTC(Number(fields.year), Number(fields.month) - 1, Number(fields.day));
  };
  const days = (day(reportDate) - day(contractInstant(start))) / 86400000;
  return `${days} calendar day(s) at report snapshot; actual downtime is recorded separately.${calendarDate(start) ? ' Exact elapsed time is not recorded.' : ''}`;
}

export function reportImagePath(reportId, imageId) {
  return `/reports/${encodeURIComponent(reportId)}/images/${encodeURIComponent(imageId)}`;
}

// FUTURE FEATURE: CUSTOMER / COMPANY DETAILS — disabled; no stored company data.
// FUTURE FEATURE: PREVIOUS MACHINE MAINTENANCE HISTORY — disabled; no history store.
