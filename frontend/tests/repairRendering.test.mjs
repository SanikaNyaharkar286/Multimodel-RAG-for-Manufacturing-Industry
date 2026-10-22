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
const { default: ChatMessage } = await server.ssrLoadModule('/src/components/chat/ChatMessage.jsx');
const { default: AdminPortal } = await server.ssrLoadModule('/src/components/AdminPortal.jsx');
const workflow = { workflow_id: 'repair-1', status: 'awaiting_done', machine: 'Haas VF-2',
  issue: 'Spindle overheating', completed_steps: [], current_step: { step_id: 'step-1', number: 1 } };
const response = { safety_warning: 'Switch off power. Use safety gloves. Follow lockout/tagout.',
  repair_workflow: workflow, blocks: [{ type: 'text', content: '### Step 1\nInspect the indicator.' }] };

test('email review link opens approvals and selects the request without granting a role', () => {
  assert.deepEqual(approvalPortalState('?admin=approvals&request=technician-1'),
    { open: true, requestUid: 'technician-1' });
  assert.equal(approvalPortalState('?admin=other').open, false);
  assert.equal(approvalPortalState('').open, false);
  assert.equal(approvalPortalState('', '#admin').open, true);
});

test('admin approvals render as a separate page with back navigation and request filters', () => {
  const html=renderToStaticMarkup(createElement(AdminPortal,{user:{},onClose(){}}));
  assert.match(html,/class="admin-page"/);
  assert.doesNotMatch(html,/admin-overlay|role="dialog"/);
  assert.match(html,/Back to chat/);
  assert.match(html,/Search technician requests/);
  assert.match(html,/Loading technician requests/);
});

test('safety is rendered before the step and active controls include Done', () => {
  const html = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: true, onRepairAction() {} }));
  assert.ok(html.indexOf('Switch off power.') < html.indexOf('Step 1'));
  assert.match(html, /role="alert"/);
  assert.match(html, />Done — Next Step<\/button>/);
  assert.match(html, />Add Details \/ Upload Image<\/button>/);
  assert.match(html, />Issue Resolved<\/button>/);
  assert.doesNotMatch(html, /Step 2/);
});

test('loading repair request shows preliminary safety guidance and preparation state', () => {
  const html = renderToStaticMarkup(createElement(ChatMessage, { message: {
    role: 'assistant', status: 'loading', safetyGuidance: {
      title: 'Safety precautions', bullets: ['Keep clear of moving parts.'],
    },
  }, loading: true }));
  assert.match(html, /Safety precautions/);
  assert.match(html, /Preparing troubleshooting steps/);
  assert.match(html, /Keep clear of moving parts/);
});

test('later repair steps hide the repeated safety banner', () => {
  const html = renderToStaticMarkup(createElement(ResponseRenderer, {
    response, repairActive: true, showSafetyWarning: false, onRepairAction() {},
  }));
  assert.doesNotMatch(html, /Safety before repair/);
  assert.match(html, /Step 1/);
});

test('real processing and independent safety are visible without an artificial delay', () => {
  const html = renderToStaticMarkup(createElement(ChatMessage, {message:{role:'assistant',status:'loading',
    displayAfter:1e15,stage:'Searching the manuals',safetyGuidance:{title:'Safety prerequisites',bullets:['Isolate stored pressure using the approved procedure.']}}}));
  assert.match(html,/Searching the manuals/);
  assert.match(html,/Isolate stored pressure/);
});

test('conditional step collects an observation before Done and does not reveal future actions', () => {
  const html=renderToStaticMarkup(createElement(ResponseRenderer,{response:{...response,repair_workflow:{...workflow,
    current_step:{...workflow.current_step,expected_result:'Observe the indicator.',observation_prompt:'What is the indicator state?',
      branches:[{label:'On',next_step_key:'two'},{label:'Off',next_step_key:'END'}]}}},repairActive:true,onRepairAction(){}}));
  assert.match(html,/What is the indicator state/);
  assert.match(html,/disabled="">Done — Next Step/);
  assert.doesNotMatch(html,/Step 2|Inspect next/);
});

test('each separate case gets its own initial safety banner', () => {
  const other={...workflow,workflow_id:'repair-2',current_step:{...workflow.current_step,step_id:'step-new'}};
  const html=renderToStaticMarkup(createElement(ChatThread,{repairWorkflow:other,messages:[
    {id:'first-case',role:'assistant',status:'done',response},
    {id:'second-case',role:'assistant',status:'done',response:{...response,repair_workflow:other}},
  ]}));
  assert.equal((html.match(/Safety before repair/g)||[]).length,2);
});

test('retired case-confirmation notices do not ask the technician to start another case', () => {
  const html=renderToStaticMarkup(createElement(ChatMessage,{message:{role:'assistant',status:'done',response:{
    case_conflict:true,blocks:[{type:'text',content:'A different repair case is already active.'}]}}}));
  assert.equal(html,'');
});

test('existing technical error messages render a simple retry notice', () => {
  const html=renderToStaticMarkup(createElement(ChatMessage,{message:{role:'assistant',status:'error',
    error:'A model, BigQuery, or asset service failed. Check the backend terminal.',retry:true},onRetry(){}}));
  assert.doesNotMatch(html,/BigQuery|backend terminal|asset service/);
  assert.match(html,/Please retry/);
  assert.match(html,/Retry request/);
});

test('information answers offer a PDF preview and resolved cases open their own documentation', () => {
  const information=renderToStaticMarkup(createElement(ResponseRenderer,{response:{session_id:'session-a',message_id:'answer-a',blocks:[{type:'text',content:'Complete specifications.'}]}}));
  assert.match(information,/>Create PDF<\/button>/);
  const report={report_id:'report-a',report_number:'MR-A',session_id:'session-a',case_id:'case-a',status:'Unresolved',
    created_at:'2026-10-22T10:00:00Z',timezone:'Asia/Calcutta',history:[],workflow:{completed_steps:[]}};
  const documentation=renderToStaticMarkup(createElement(ResponseRenderer,{response:{blocks:[],documentation:report}}));
  assert.match(documentation,/Editable documentation|Documentation preview/);
  assert.match(documentation,/MR-A/);
  assert.match(documentation,/Unresolved/);
  assert.match(documentation,/>Download PDF<\/button>/);
  assert.match(documentation,/>Back to Conversation<\/button>/);
});

test('historical steps do not expose controls and pending requests disable actions', () => {
  const historical = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: false, onRepairAction() {} }));
  assert.doesNotMatch(historical, /<button/);
  const busy = renderToStaticMarkup(createElement(ResponseRenderer, { response, repairActive: true, loading: true, onRepairAction() {} }));
  assert.equal((busy.match(/disabled=""/g) || []).length, 4);
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

test('information follow-up shows all content while the active repair remains available', () => {
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
  assert.equal((html.match(/>Done — Next Step<\/button>/g) || []).length, 1);
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
