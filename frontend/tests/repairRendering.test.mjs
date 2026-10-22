import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { approvalPortalState } from '../src/auth/approvalLink.js';

// Use the actual JSX renderer and Vite environment without starting an HTTP listener.
const server = await createServer({ server: { middlewareMode: true }, appType: 'custom',
  customLogger: { info() {}, warn() {}, warnOnce() {}, error() {}, clearScreen() {}, hasErrorLogged() { return false; }, hasWarned: false } });
after(() => server.close());
const { default: ResponseRenderer } = await server.ssrLoadModule('/src/components/chat/ResponseRenderer.jsx');
const { default: ChatThread } = await server.ssrLoadModule('/src/components/chat/ChatThread.jsx');
const workflow = { workflow_id: 'repair-1', status: 'awaiting_done', machine: 'Haas VF-2',
  issue: 'Spindle overheating', completed_steps: [], current_step: { step_id: 'step-1', number: 1 } };
const response = { safety_warning: 'Switch off power. Use safety gloves. Follow lockout/tagout.',
  repair_workflow: workflow, blocks: [{ type: 'text', content: '### Step 1\nInspect the indicator.' }] };

test('email review link opens approvals and selects the request without granting a role', () => {
  assert.deepEqual(approvalPortalState('?admin=approvals&request=technician-1'),
    { open: true, requestUid: 'technician-1' });
  assert.equal(approvalPortalState('?admin=other').open, false);
  assert.equal(approvalPortalState('').open, false);
});

test('safety is rendered before the step and active controls include Done', () => {
  const html = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: true, onRepairAction() {} }));
  assert.ok(html.indexOf('Switch off power.') < html.indexOf('Step 1'));
  assert.match(html, /role="alert"/);
  assert.match(html, />Done<\/button>/);
  assert.match(html, />Did not fix \/ Report issue<\/button>/);
  assert.match(html, />Issue resolved<\/button>/);
  assert.doesNotMatch(html, /Step 2/);
});

test('later repair steps hide the repeated safety banner', () => {
  const html = renderToStaticMarkup(createElement(ResponseRenderer, {
    response, repairActive: true, showSafetyWarning: false, onRepairAction() {},
  }));
  assert.doesNotMatch(html, /Safety before repair/);
  assert.match(html, /Step 1/);
});

test('historical steps do not expose controls and pending requests disable actions', () => {
  const historical = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: false, onRepairAction() {} }));
  assert.doesNotMatch(historical, /<button/);
  const busy = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: true, loading: true, onRepairAction() {} }));
  assert.equal((busy.match(/disabled=""/g) || []).length, 3);
});

test('maintenance report renders the technician-confirmed fields', () => {
  const html = renderToStaticMarkup(createElement(ResponseRenderer, { response: {
    blocks: [], maintenance_report: { machine: 'Haas VF-2', issue: 'Spindle overheating',
      cause: 'Lubrication blockage', action_taken: 'Cleaned lubrication line', status: 'Resolved',
      created_at: '2026-10-21T10:00:00Z' },
  } }));
  for (const text of ['Maintenance report', 'Haas VF-2', 'Spindle overheating', 'Lubrication blockage',
    'Cleaned lubrication line', 'Resolved']) assert.ok(html.includes(text));
  assert.match(html, />Edit report<\/button>/);
  assert.match(html, />Download report<\/button>/);
});

test('information answer shows all content and hides old repair controls in the thread', () => {
  const information = { blocks: [{ type: 'text', content: 'Full component explanation.' },
    { type: 'text', content: 'Additional supported specifications.' }] };
  const html = renderToStaticMarkup(createElement(ChatThread, {
    repairWorkflow: workflow, onRepairAction() {}, messages: [
      { id: 'repair', role: 'assistant', status: 'done', response },
      { id: 'info', role: 'assistant', status: 'done', response: information },
    ],
  }));
  assert.ok(html.includes('Full component explanation.'));
  assert.ok(html.includes('Additional supported specifications.'));
  assert.doesNotMatch(html, /<button/);
  const answer = renderToStaticMarkup(createElement(ResponseRenderer, { response: information }));
  assert.doesNotMatch(answer, /repair-safety|Maintenance report|Done/);
  const normalThread = renderToStaticMarkup(createElement(ChatThread, {
    repairWorkflow: null, onRepairAction() {}, messages: [
      { id: 'information-only', role: 'assistant', status: 'done', response: information },
    ],
  }));
  assert.ok(normalThread.includes('Full component explanation.'));
  assert.doesNotMatch(normalThread, /<button|repair-safety|Maintenance report/);
});
