import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { reviewFields, reviewSteps, reportEdit, hasReportEdits, validateReview,
  issueDuration, localDateTime, confirmedDateTime, reportImagePath, RESOLUTION_STATUSES, updateStepConfirmation } from '../src/components/chat/reportReview.js';

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom',
  customLogger: { info() {}, warn() {}, warnOnce() {}, error() {}, clearScreen() {}, hasErrorLogged() { return false; }, hasWarned: false } });
after(() => server.close());
const { default: Documentation, StepReview } = await server.ssrLoadModule('/src/components/chat/Documentation.jsx');
const { default: ReportEvidence } = await server.ssrLoadModule('/src/components/chat/ReportEvidence.jsx');
const { default: ResponseRenderer } = await server.ssrLoadModule('/src/components/chat/ResponseRenderer.jsx');
const report = { report_id: 'report-a', report_number: 'TMR-A', revision: 3, case_id: 'case-a',
  created_at: '2026-10-28T10:00:00Z', timezone: 'Asia/Calcutta', technician_name: 'Signed-in Technician', technician_email: 'tech@example.com',
  model: 'HAB-3232', status: 'Under Investigation', steps: [
    { step_id: 'step-1', number: 1, title: 'Inspect clamp', instruction: 'Inspect the vise using the approved procedure.', status: 'Completed', observation: 'Clamp does not close.', result: 'Failed', completed_at: '2026-10-28T15:00:00+05:30' },
    { step_id: 'step-2', number: 2, title: 'Check supply', status: 'Pending' },
    { step_id: 'step-3', number: 3, title: 'Inspect assembly', status: 'Skipped', result: 'Not tested' },
  ] };

test('review shows grouped fields, authenticated identity, unknowns and no company or history section', () => {
  const html = renderToStaticMarkup(createElement(Documentation, { response: { documentation: report } }));
  for (const label of ['Technical Service', 'Machine identification', 'Technician and service', 'Fault information', 'Repair and spare parts', 'Troubleshooting log', 'Signed-in Technician', 'tech@example.com', 'HAB-3232', 'No confirmed repair recorded.', 'Root cause not confirmed.', 'No photographic evidence attached.', 'Issue duration: Not reported', 'Signature Pending', 'Not authorized.']) assert.ok(html.includes(label), label);
  assert.doesNotMatch(html, /name="(?:technician_name|technician_email|uid|return_to_service)"|Company Details|Maintenance History|Approved<\/option>/);
  assert.match(html, /<details class="report-review-group"/);
  assert.match(html, /1 completed \/ 3 recorded/);
});

test('every supported resolution remains independent of operating condition and authorization', () => {
  for (const status of RESOLUTION_STATUSES) {
    const html = renderToStaticMarkup(createElement(Documentation, { response: { documentation: { ...report, status, operational_status: 'Not Operational', return_to_service: 'Approved' } } }));
    assert.ok(html.includes(`${status}</option>`));
    assert.match(html, /value="Not Operational" selected=""/);
    assert.match(html, /Return-to-service authorization: Not authorized/);
    assert.doesNotMatch(html, /Approved/);
  }
});

test('recommended, pending and skipped steps never become completed during normalization or save', () => {
  const input = { ...report, steps: [...report.steps, { instruction: 'Recommendation without an ID' }, { step_id: 'bad-status', status: 'recommended' }] };
  const steps = reviewSteps(input);
  assert.deepEqual(steps.map(step => step.status), ['Completed', 'Pending', 'Skipped', 'Pending', 'Pending']);
  assert.equal(steps[1].result, 'Not tested');
  assert.equal(steps[1].completed_at, '');
  assert.equal(steps[3].step_id, undefined);
  assert.equal(hasReportEdits(reportEdit(input, reviewFields(input), steps)), false);
});

test('payload contains only editable changes and confirmations with actual server step IDs', () => {
  const fields = { ...reviewFields(report), serial_number: 'SN-100', status: 'Awaiting OEM Support', uid: 'forged', technician_name: 'forged', return_to_service: 'Approved' };
  const steps = reviewSteps(report);
  steps[1] = { ...steps[1], observation: 'Supply not inspected yet.', notes: 'Awaiting isolation.' };
  const edit = reportEdit(report, fields, steps);
  assert.deepEqual(edit.fields, { serial_number: 'SN-100', status: 'Awaiting OEM Support' });
  assert.equal(edit.revision, 3);
  assert.deepEqual(edit.step_confirmations, [{ step_id: 'step-2', status: 'Pending', observation: 'Supply not inspected yet.', result: 'Not tested', notes: 'Awaiting isolation.', parts_used: '', action_performed: '', completed_at: '' }]);
  assert.equal(hasReportEdits(edit), true);
  assert.equal('uid' in edit, false);
});

test('explicit completion preserves unrecorded results, parts, actions and completion date', () => {
  const steps = reviewSteps(report);
  steps[1].status = 'Completed';
  const [confirmation] = reportEdit(report, reviewFields(report), steps).step_confirmations;
  assert.equal(confirmation.status, 'Completed');
  for (const key of ['completed_at', 'action_performed', 'parts_used', 'observation']) assert.equal(confirmation[key], '');
  assert.equal(confirmation.result, 'Not tested');
});

test('moving performed work to an unperformed status clears only execution assertions', () => {
  const performed = { ...reviewSteps(report)[0], action_performed: 'Inspected clamp', parts_used: 'One seal', notes: 'Retained note' };
  for (const status of ['Pending', 'Skipped', 'Not performed']) {
    const step = updateStepConfirmation(performed, 'status', status);
    assert.equal(step.status, status);
    for (const key of ['action_performed', 'parts_used', 'completed_at']) assert.equal(step[key], '');
    assert.equal(step.result, 'Not tested');
    assert.equal(step.observation, performed.observation);
    assert.equal(step.notes, 'Retained note');
    assert.equal(performed.parts_used, 'One seal');
    assert.equal(validateReview(reviewFields(report), [step], Date.parse('2026-10-28T23:00:00Z')), '');
  }
});

test('unperformed steps disable execution fields and reject stale performed-work payloads', () => {
  const steps = reviewSteps(report);
  const html = renderToStaticMarkup(createElement(StepReview, { steps: [steps[1]], onChange() {} }));
  for (const name of ['action_performed', 'parts_used', 'completed_at', 'result']) {
    const control = html.match(new RegExp(`<(?:input|textarea|select)\\b[^>]*name="${name}"[^>]*>`))?.[0];
    assert.ok(control, name);
    assert.match(control, /disabled=""/);
  }
  steps[1].parts_used = 'Unconfirmed part';
  assert.match(validateReview(reviewFields(report), steps, Date.parse('2026-10-28T23:00:00Z')), /cannot contain performed/);
});

test('dates preserve timezone-aware instants, clear unknown timestamps, and reject invalid service chronology', () => {
  const value = '2026-10-28T10:20:30+05:30';
  assert.equal(confirmedDateTime(localDateTime(value)), new Date(value).toISOString());
  assert.equal(confirmedDateTime(''), '');
  const fields = reviewFields(report);
  const now = Date.parse('2026-10-28T23:00:00Z');
  assert.equal(validateReview(fields, reviewSteps(report), now), '');
  fields.fault_started_at = '2026-10-28T10:20';
  assert.match(validateReview(fields, []), /timezone/);
  fields.fault_started_at = value;
  fields.service_start = '2026-10-28T11:00:00Z'; fields.service_end = '2026-10-28T10:00:00Z';
  assert.match(validateReview(fields, []), /cannot precede/);
  fields.service_end = ''; fields.installation_date = '2026-02-30';
  assert.match(validateReview(fields, []), /valid date/);
});

test('future actual dates are rejected while expected resolution may be future and calendar dates retain no invented time', () => {
  const fields = reviewFields(report);
  const now = Date.parse('2026-10-28T23:00:00Z');
  fields.service_start = '2026-11-02T00:00:00Z';
  assert.match(validateReview(fields, [], now), /cannot be in the future/);
  fields.service_start = '2026-10-27'; fields.expected_resolution_at = '2026-11-02T00:00:00Z';
  assert.equal(validateReview(fields, [], now), '');
  assert.match(issueDuration('2026-10-27', report.created_at), /Exact elapsed time is not recorded/);
  fields.functional_test = 'Passed';
  assert.match(validateReview(fields, [], now), /valid functional test/);
  fields.functional_test = 'Pass'; fields.escalation_status = 'Submitted';
  assert.match(validateReview(fields, [], now), /valid recorded escalation status/);
  fields.escalation_status = 'Draft';
  assert.equal(validateReview(fields, [], now), '');
});

test('duration uses the confirmed fault instant and report snapshot, separate from downtime', () => {
  assert.equal(issueDuration('', report.created_at), 'Not reported');
  assert.match(issueDuration('2026-10-26T15:30:00+05:30', report.created_at), /^2 calendar day/);
  assert.match(issueDuration('2026-10-29T10:00:00Z', report.created_at), /after this report snapshot/);
});

test('long procedures, precautions, pending steps and references remain visible in the log', () => {
  const instruction = 'Approved manual instruction. '.repeat(200);
  const steps = reviewSteps({ steps: [{ step_id: 'real-id', instruction, precautions: ['Isolate power.'], sources: [{ document_name: 'Manual.pdf', page_number: 12 }], evidence_ids: ['image-a'] }] });
  const html = renderToStaticMarkup(createElement(StepReview, { steps, onChange() {} }));
  assert.ok(html.includes(instruction));
  for (const text of ['Pending', 'Not tested', 'Isolate power.', 'Manual.pdf', 'PDF page 12', 'image-a']) assert.ok(html.includes(text));
  assert.doesNotMatch(html, /value="Completed" selected/);
});

test('manual diagrams and technician photos show captions, source type and step association', () => {
  const html = renderToStaticMarkup(createElement(ReportEvidence, { report: { ...report, evidence: [
    { image_id: 'diagram-1', caption: 'Vise diagram', source_type: 'manual_diagram', step_id: 'step-1', source: { document_name: 'Manual.pdf', page_number: 12 } },
    { image_id: 'photo-1', caption: 'Fault photograph', description: 'Clamp before inspection', source_type: 'technician_upload' },
  ] } }));
  for (const text of ['Vise diagram', 'manual_diagram', 'step-1', 'Manual.pdf', 'Fault photograph', 'technician_upload', 'Clamp before inspection']) assert.ok(html.includes(text));
  assert.doesNotMatch(html, /<img[^>]*src="(?:http|\/reports)/);
  assert.equal(reportImagePath('report/a', 'photo/b'), '/reports/report%2Fa/images/photo%2Fb');
});

test('recorded guidance preserves information answers and table values without claiming completed work', () => {
  const html = renderToStaticMarkup(createElement(Documentation, { response: { documentation: { ...report,
    manual_guidance: [{ blocks: [{ type: 'text', content: 'Manual-supported possible cause.' }, { type: 'table', columns: ['Reference'], rows: [['12 bar']] }, { type: 'image', src: '/images/diagram' }], sources: [{ document_name: 'Manual.pdf', page_number: 5 }] }],
  } } }));
  for (const value of ['Manual-supported possible cause.', '<td>12 bar</td>', 'Manual.pdf', 'Recorded manual guidance']) assert.ok(html.includes(value));
  assert.doesNotMatch(html, /src="\/images\/diagram"/);
});

test('report action waits for troubleshooting to end while guided controls stay available', () => {
  const response = { session_id: 's', message_id: 'm', repair_workflow: { workflow_id: 'case-a', status: 'awaiting_done', current_step: { step_id: 'step-1' } }, blocks: [] };
  const html = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: true, onRepairAction() {} }));
  assert.doesNotMatch(html, /Generate Maintenance Report/);
  assert.match(html, /Done — Next Step/);
  const activeAfterInformation = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: true, allowDocumentation: false, onRepairAction() {} }));
  assert.doesNotMatch(activeAfterInformation, /Generate Maintenance Report/);
  const savingStep = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: true, loading: true, allowDocumentation: false, onRepairAction() {} }));
  assert.doesNotMatch(savingStep, /Generate Maintenance Report/);
  for (const props of [{ allowDocumentation: false }, { response: { ...response, documentable: false } }]) {
    assert.doesNotMatch(renderToStaticMarkup(createElement(ResponseRenderer, { response, ...props })), /Generate Maintenance Report/);
  }
  for (const status of ['resolved', 'unresolved']) {
    const finalResponse = { ...response, repair_workflow: { ...response.repair_workflow, status, current_step: null } };
    assert.match(renderToStaticMarkup(createElement(ResponseRenderer, { response: finalResponse })), />Generate Maintenance Report<\/button>/);
    for (const props of [{ allowDocumentation: false }, { loading: true }, { documentationMode: true }]) {
      assert.doesNotMatch(renderToStaticMarkup(createElement(ResponseRenderer, { response: finalResponse, ...props })), /Generate Maintenance Report/);
    }
  }
});

// Exercise the actual HTTP module with only Firebase and runtime config stubbed.
const auth = { currentUser: { getIdToken: async force => force ? 'refreshed' : 'initial' } };
globalThis.__reportTestAuth = auth;
const source = (await readFile(new URL('../src/services/documentationApi.js', import.meta.url), 'utf8'))
  .replace("import { firebaseAuth } from '../auth/firebase';", 'const firebaseAuth = globalThis.__reportTestAuth;')
  .replace("import { APP_CONFIG } from '../config/appConfig';", 'const APP_CONFIG = { apiBaseUrl: "http://localhost:8000" };');
const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
delete globalThis.__reportTestAuth;

test('report edits and evidence use authenticated requests, with one expired-token refresh', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, options });
    return calls.length === 1 ? { ok: false, status: 401 } : { ok: true, json: async () => ({ revision: 4 }), blob: async () => new Blob(['photo']) };
  });
  const edit = { revision: 3, fields: { notes: 'Confirmed observation' } };
  assert.deepEqual(await api.documentRequest('/reports/report-a', edit), { revision: 4 });
  assert.equal(calls[0].options.headers.Authorization, 'Bearer initial');
  assert.equal(calls[1].options.headers.Authorization, 'Bearer refreshed');
  assert.deepEqual(JSON.parse(calls[1].options.body), edit);
  assert.ok(await api.documentRequest(reportImagePath('report-a', 'photo-1'), null, true) instanceof Blob);
  assert.equal(calls[2].options.method, 'GET');
  assert.equal(calls[2].options.headers.Authorization, 'Bearer initial');
});

test('conflicts and unauthorized errors preserve status for safe UI recovery', async t => {
  for (const status of [403, 409]) {
    t.mock.method(globalThis, 'fetch', async () => ({ ok: false, status, json: async () => ({ detail: status === 409 ? 'Reopen before saving.' : 'Not authorized.' }) }));
    await assert.rejects(api.documentRequest('/reports/report-a'), error => error.status === status);
  }
});

test('PDF download uses the authenticated snapshot endpoint and server report number', async t => {
  let clicked = false;
  const link = { click() { clicked = true; } };
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, 'http://localhost:8000/reports/report-a/pdf');
    assert.equal(options.headers.Authorization, 'Bearer initial');
    return { ok: true, blob: async () => new Blob(['%PDF']) };
  });
  t.mock.method(URL, 'createObjectURL', () => 'blob:report-pdf');
  t.mock.method(URL, 'revokeObjectURL', () => {});
  const originalDocument = globalThis.document;
  globalThis.document = { createElement: () => link };
  t.after(() => { if (originalDocument) globalThis.document = originalDocument; else delete globalThis.document; });
  t.mock.method(globalThis, 'setTimeout', callback => { callback(); return 1; });
  await api.downloadDocument(report);
  assert.equal(clicked, true);
  assert.equal(link.download, 'TMR-A.pdf');
});
