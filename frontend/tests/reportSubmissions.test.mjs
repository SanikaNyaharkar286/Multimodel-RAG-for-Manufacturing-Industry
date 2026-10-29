import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { transformWithOxc } from 'vite';
import * as review from '../src/components/chat/reportReview.js';
import { approvalPortalState } from '../src/auth/approvalLink.js';
import * as display from '../src/components/reports/reportDisplay.js';

// Run the real component handlers and state transitions. Only React scheduling,
// network boundaries and unrelated child components are replaced, not handlers.
async function componentHarness(file, imports) {
  const slots = [], pending = [];
  let cursor = 0;
  const changed = (a, b) => !a || !b || a.length !== b.length || a.some((item, i) => item !== b[i]);
  const hooks = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
    },
    useRef(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { current: initial }; return slots[index]; },
    useEffect(effect, deps) {
      const index = cursor++;
      if (changed(slots[index]?.deps, deps)) pending.push(() => {
        slots[index]?.cleanup?.(); slots[index] = { deps, cleanup: effect() };
      });
    },
    useCallback(callback, deps) {
      const index = cursor++;
      if (changed(slots[index]?.deps, deps)) slots[index] = { deps, callback };
      return slots[index].callback;
    },
  };
  const key = `__reportSubmissionHarness${Math.random().toString(36).slice(2)}`;
  const runtime = { jsx: (type, props, key) => ({ type, props: props || {}, key }), jsxs: (type, props, key) => ({ type, props: props || {}, key }) };
  runtime.Fragment = 'fragment';
  const urls = [];
  globalThis[key] = { imports: { react: hooks, 'react/jsx-runtime': runtime, ...imports },
    window: { location: { pathname: '/', search: '' }, history: { replaceState: (...args) => urls.push(args[2]) } } };
  let source = await readFile(new URL(file, import.meta.url), 'utf8');
  source = (await transformWithOxc(source, 'test-component.jsx', { jsx: { runtime: 'automatic' } })).code;
  source = source.replace(/import\s+['"][^'"]+['"];?/g, '').replace(/import\s+([^;]+?)\s+from\s+['"]([^'"]+)['"];?/g, (_, bindings, name) => {
    const access = `globalThis.${key}.imports[${JSON.stringify(name)}]`;
    if (bindings.trim().startsWith('{')) return `const ${bindings.replace(/\bas\b/g, ':')} = ${access};`;
    const [main, extra] = bindings.split(/,\s*(?=\{)/);
    return `const ${main.trim()} = ${access}.default;${extra ? `const ${extra.replace(/\bas\b/g, ':')} = ${access};` : ''}`;
  });
  source = `const document = { body: { style: {} } }; const window = globalThis.${key}.window;\n${source}`;
  const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
  return {
    urls,
    render(props, name = 'default') { cursor = 0; return module[name](props); },
    async flush() { for (const effect of pending.splice(0)) effect(); for (let i = 0; i < 6; i++) await Promise.resolve(); },
    close() { for (const item of slots) item?.cleanup?.(); delete globalThis[key]; },
  };
}

function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (!tree || typeof tree !== 'object') return [];
  return [tree, ...nodes(tree.props?.children)];
}
const text = tree => Array.isArray(tree) ? tree.map(text).join('') : typeof tree === 'object' && tree ? text(tree.props?.children) : tree == null ? '' : String(tree);
const button = (tree, label) => nodes(tree).find(node => node.type === 'button' && text(node) === label);
const report = { report_id: 'report-a', report_number: 'TMR-20261029-ABC', revision: 2, created_at: '2026-10-28T10:00:00Z',
  status: 'Unresolved', machine: 'HAB-3232', issue: 'Pump not starting', steps: [{ step_id: 'step-a', instruction: 'Inspect indicator.', status: 'Pending', result: 'Not tested' }] };
const submission = { report_id: 'report-a', submitted_at: '2026-10-29T09:00:00Z', review_status: 'new', email_status: 'pending' };
function documentImports(request, submit) {
  return {
    '../../services/documentationApi': { documentRequest: request, downloadDocument: async () => {} },
    '../../services/reportSubmissionsApi': { submitReport: submit },
    '../../hooks/useDialogFocus': { default: () => ({ current: null }) },
    './ReportEvidence': { default: 'evidence' }, './ResponseRenderer': { default: 'response' },
    './reportReview': review, '../reports/ReportPdf': { default: 'pdf', SubmissionStatus: 'submission-status' },
  };
}

test('Send to Admin saves edits, prevents double clicks, shows pending state, and restores durable submission after refresh', async () => {
  const calls = [];
  let accept;
  let canonical = structuredClone(report);
  const request = async (path, body) => {
    calls.push({ path, body });
    if (body) canonical = { ...canonical, ...body.fields, revision: 3 };
    return canonical;
  };
  const send = async current => { calls.push({ submit: current }); return new Promise(resolve => { accept = resolve; }); };
  const harness = await componentHarness('../src/components/chat/Documentation.jsx', documentImports(request, send));
  const props = { response: { documentation: report } };
  try {
    harness.render(props); await harness.flush();
    let tree = harness.render(props);
    const issueField = nodes(tree).find(node => node.props?.definition?.[0] === 'issue');
    issueField.props.onChange('issue', 'Confirmed issue description');
    tree = harness.render(props);
    const action = button(tree, 'Send to Admin');
    const sending = action.props.onClick();
    await action.props.onClick();
    for (let i = 0; i < 4; i++) await Promise.resolve();
    tree = harness.render(props);
    assert.equal(button(tree, 'Submitting…').props.disabled, true);
    assert.equal(calls.filter(call => call.submit).length, 1);
    assert.equal(calls.find(call => call.submit).submit.revision, 3);
    assert.equal(calls.find(call => call.body).body.fields.issue, 'Confirmed issue description');
    assert.deepEqual(calls.find(call => call.body).body.step_confirmations || [], [], 'Pending steps are not silently confirmed');
    accept(submission); await sending;
    tree = harness.render(props);
    assert.equal(button(tree, 'Submitted to Admin').props.disabled, true);
    assert.match(text(tree), /Report submitted to admin successfully/);
    assert.equal(nodes(tree).find(node => node.props?.className === 'report-review-form').props.disabled, true);
    canonical = { ...canonical, submission };
  } finally { harness.close(); }
  const refreshed = await componentHarness('../src/components/chat/Documentation.jsx', documentImports(request, send));
  try {
    refreshed.render(props); await refreshed.flush();
    assert.equal(button(refreshed.render(props), 'Submitted to Admin').props.disabled, true);
    assert.equal(calls.filter(call => call.submit).length, 1, 'Refresh must not send again');
  } finally { refreshed.close(); }
});

test('a lost submission response reconciles server state and never automatically sends again', async () => {
  let canonical = structuredClone(report), attempts = 0;
  const harness = await componentHarness('../src/components/chat/Documentation.jsx', documentImports(async () => canonical, async () => {
    attempts++; canonical = { ...canonical, submission }; throw new Error('Network disconnected');
  }));
  const props = { response: { documentation: report } };
  try {
    harness.render(props); await harness.flush();
    await button(harness.render(props), 'Send to Admin').props.onClick();
    assert.equal(attempts, 1);
    assert.equal(button(harness.render(props), 'Submitted to Admin').props.disabled, true);
  } finally { harness.close(); }
});

test('admin review uses server version, preserves comments on conflict, and records no technical edits', async () => {
  const edits = [];
  const harness = await componentHarness('../src/components/reports/AdminReports.jsx', {
    '../../services/reportSubmissionsApi': {
      adminReport: async () => ({ submission: { ...submission, version: 4, report_number: report.report_number }, report }),
      reviewReport: async (id, edit) => { edits.push({ id, edit }); const error = new Error('Review changed. Reload before saving.'); error.status = 409; throw error; },
    },
    './ReportPdf': { default: 'pdf', reportDate: value => value || 'Not recorded', reviewLabel: value => value, SubmissionStatus: 'submission' },
    './reportDisplay': { reportDate: value => value || 'Not recorded', reviewLabel: value => value },
    '../chat/ResponseRenderer': { default: 'response' },
  });
  const props = { reportId: report.report_id, onBack() {} };
  try {
    harness.render(props, 'AdminReportDetail'); await harness.flush();
    let tree = harness.render(props, 'AdminReportDetail');
    nodes(tree).find(node => node.type === 'textarea').props.onChange({ target: { value: 'Inspect spare part.' } });
    tree = harness.render(props, 'AdminReportDetail');
    await nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
    tree = harness.render(props, 'AdminReportDetail');
    assert.deepEqual(edits, [{ id: report.report_id, edit: { review_status: 'new', admin_comment: 'Inspect spare part.', version: 4 } }]);
    assert.equal(nodes(tree).find(node => node.type === 'textarea').props.value, 'Inspect spare part.');
    assert.equal(button(tree, 'Save review').props.disabled, true);
    assert.ok(button(tree, 'Reload latest review (discard edits)'));
  } finally { harness.close(); }
});

test('report email deep link opens the admin portal without changing registration link behavior', () => {
  assert.equal(approvalPortalState('?admin=reports&report=report-a').open, true);
  assert.deepEqual(approvalPortalState('?admin=approvals&request=user-a'), { open: true, requestUid: 'user-a' });
});

test('report API uses authenticated existing transport, PATCH review, and canonical record IDs', async () => {
  const calls = [];
  globalThis.__submissionApiTransport = (...args) => { calls.push(args); return Promise.resolve({}); };
  try {
    const source = (await readFile(new URL('../src/services/reportSubmissionsApi.js', import.meta.url), 'utf8')).replace(
      "import { documentRequest } from './documentationApi';", 'const documentRequest = globalThis.__submissionApiTransport;');
    const api = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    await api.submitReport(report);
    await api.reviewReport('report/a', { version: 2, review_status: 'reviewed', admin_comment: 'Recorded' });
    await api.markNotificationRead('notification/a');
    await api.myReports(2);
    assert.deepEqual(calls[0], ['/reports/report-a/submit', { revision: 2 }]);
    assert.equal(calls[1][0], '/admin/reports/report%2Fa/review'); assert.equal(calls[1][3], 'PATCH');
    assert.equal(calls[2][0], '/admin/notifications/notification%2Fa/read'); assert.equal(calls[2][3], 'PATCH');
    assert.equal(calls[3][0], '/reports/my?page=2&page_size=20');
  } finally { delete globalThis.__submissionApiTransport; }
});

test('admin bell shows persistent unread count, reads independently, and opens canonical report', async () => {
  const item = { notification_id: 'n-a', type: 'maintenance_report_submitted', report_id: 'report-a', is_read: false };
  let unread = 3;
  const read = [];
  const harness = await componentHarness('../src/components/AdminPortal.jsx', {
    '../services/authApi': { getAdminRequests: async () => ({ requests: [] }), decideAdminRequest: async () => {} },
    '../config/appConfig': { APP_CONFIG: { appName: 'Copilot' } },
    './layout/GearLogo': { default: 'gear' }, './reports/AdminReports': { default: 'reports' },
    './reports/AdminNotifications': { default: 'notifications' },
    '../services/reportSubmissionsApi': {
      adminNotifications: async () => ({ unread_count: unread, notifications: [item] }),
      markNotificationRead: async id => { read.push(id); unread--; item.is_read = true; },
      markAllNotificationsRead: async () => { unread = 0; },
    },
  });
  const props = { user: {}, initialTab: 'reports', onClose() {} };
  try {
    harness.render(props); await harness.flush();
    let tree = harness.render(props);
    let bell = nodes(tree).find(node => node.props?.className === 'notification-bell');
    assert.equal(bell.props['aria-label'], 'Notifications, 3 unread');
    bell.props.onClick(); tree = harness.render(props);
    const inbox = nodes(tree).find(node => node.type === 'notifications');
    await inbox.props.onOpen(item); tree = harness.render(props);
    assert.deepEqual(read, ['n-a']);
    assert.equal(nodes(tree).find(node => node.type === 'reports').props.selectedId, 'report-a');
    assert.match(harness.urls.at(-1), /admin=reports&report=report-a/);
    bell = nodes(tree).find(node => node.props?.className === 'notification-bell');
    assert.equal(bell.props['aria-label'], 'Notifications, 2 unread');
    bell.props.onClick(); tree = harness.render(props);
    await nodes(tree).find(node => node.type === 'notifications').props.onReadAll();
    assert.equal(nodes(harness.render(props)).find(node => node.props?.className === 'notification-bell').props['aria-label'], 'Notifications, 0 unread');
  } finally { harness.close(); }
});

test('My Submitted Reports displays backend review/comment state and authenticated PDF controls', async () => {
  const harness = await componentHarness('../src/components/reports/MySubmittedReports.jsx', {
    '../../services/reportSubmissionsApi': { myReports: async () => ({ total: 1, reports: [{ ...report, ...submission, machine_name: report.machine, issue_description: report.issue, review_status: 'reviewed', admin_comment: 'Inspection recorded.' }] }) },
    './ReportPdf': { default: 'pdf' }, './reportDisplay': display,
  });
  const props = { onBack() {}, onCurrentReport() {} };
  try {
    harness.render(props); await harness.flush();
    const tree = harness.render(props);
    assert.match(text(tree), /Reviewed/); assert.match(text(tree), /Inspection recorded/);
    assert.match(text(tree), /Email queued/); assert.match(text(tree), /HAB-3232/);
    assert.equal(nodes(tree).find(node => node.type === 'pdf').props.report.report_id, 'report-a');
  } finally { harness.close(); }
});
