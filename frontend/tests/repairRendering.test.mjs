import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { approvalPortalState } from '../src/auth/approvalLink.js';
// Keep report-contract coverage in the existing frontend test command.
import './reportReview.test.mjs';

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

test('verification guidance is shown before manual images and Done requires safety confirmation', () => {
  const html=renderToStaticMarkup(createElement(ResponseRenderer,{response:{...response,repair_workflow:{...workflow,
    current_step:{...workflow.current_step,expected_result:'Oil reaches the approved mark.'}},
    blocks:[{type:'text',content:'Check the oil gauge.'},{type:'image',src:'/images/gauge',caption:'Oil gauge'}]},
    repairActive:true,onRepairAction(){}}));
  assert.ok(html.indexOf('Check the oil gauge.') < html.indexOf('How to verify'));
  assert.ok(html.indexOf('Oil reaches the approved mark.') < html.indexOf('Oil gauge'));
  assert.match(html,/disabled="">Done — Next Step<\/button>/);
});

test('guided steps preserve interleaved details, figures and original table rows', () => {
  const html = renderToStaticMarkup(createElement(ResponseRenderer, { response: {
    ...response,
    blocks: [
      { type: 'text', content: 'Read the pressure gauge.' },
      { type: 'image', src: '/images/gauge', caption: 'Pressure gauge', description:
        '- Description: Gauge on Machine Unit_X.\n- Callout 1 — pressure dial.\n- Technician check: Compare the reading with 12 bar.' },
      { type: 'text', content: 'Interpret the gauge using this reference.' },
      { type: 'table', columns: ['Pressure'], rows: [['12 bar']] },
      { type: 'image', src: '/images/scale', caption: 'Gauge scale', description:
        '- Description: Scale detail.\n- Technician check: Identify the correct unit.' },
    ],
  }, repairActive: true, onRepairAction() {} }));
  assert.ok(html.indexOf('Read the pressure gauge.') < html.indexOf('<strong>Pressure gauge</strong>'));
  assert.ok(html.indexOf('<strong>Pressure gauge</strong>') < html.indexOf('Interpret the gauge'));
  assert.ok(html.indexOf('Interpret the gauge') < html.indexOf('<td>12 bar</td>'));
  assert.ok(html.indexOf('<td>12 bar</td>') < html.indexOf('<strong>Gauge scale</strong>'));
  assert.match(html, /Gauge on Machine Unit_X/);
  assert.match(html, /Callout 1 — pressure dial/);
  assert.match(html, /<strong>Technician check:<\/strong> Compare the reading with 12 bar/);
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
  assert.match(information,/>Download PDF<\/button>/);
  const report={report_id:'report-a',report_number:'MR-A',session_id:'session-a',case_id:'case-a',status:'Unresolved',
    created_at:'2026-10-22T10:00:00Z',timezone:'Asia/Calcutta',history:[],workflow:{completed_steps:[]}};
  const documentation=renderToStaticMarkup(createElement(ResponseRenderer,{response:{blocks:[],documentation:report}}));
  assert.match(documentation,/Editable documentation|Documentation preview/);
  assert.match(documentation,/MR-A/);
  assert.match(documentation,/Unresolved/);
  assert.match(documentation,/>Download PDF<\/button>/);
  assert.match(documentation,/>Back to Conversation<\/button>/);
});

test('ongoing repairs hide report generation and information PDF appears only after the latest answer', () => {
  const activeStep = {...response, session_id:'session-a', message_id:'step-answer'};
  const active = renderToStaticMarkup(createElement(ResponseRenderer, {
    response:activeStep, allowDocumentation:true,
  }));
  assert.doesNotMatch(active, /Create PDF/);
  assert.doesNotMatch(active, /Generate Maintenance Report/);

  const thread = renderToStaticMarkup(createElement(ChatThread, {messages:[
    {id:'answer-one', role:'assistant', status:'done', response:{
      session_id:'session-a', message_id:'answer-one', blocks:[{type:'text',content:'First answer.'}],
    }},
    {id:'answer-two', role:'assistant', status:'done', response:{
      session_id:'session-a', message_id:'answer-two', blocks:[{type:'text',content:'Final answer.'}],
    }},
  ]}));
  assert.equal((thread.match(/>Create PDF<\/button>/g) || []).length, 1);
  assert.ok(thread.indexOf('Final answer.') < thread.indexOf('Create PDF'));
});

test('multi-step conversation shows one report action only after the final outcome', () => {
  const stepResponse = number => ({ ...response, session_id: 'session-a', message_id: `step-${number}`,
    repair_workflow: { ...workflow, current_step: { step_id: `step-${number}`, number } } });
  const messages = [1, 2, 3].map(number => ({ id: `step-${number}`, role: 'assistant', status: 'done', response: stepResponse(number) }));
  const active = renderToStaticMarkup(createElement(ChatThread, {
    messages, repairWorkflow: stepResponse(3).repair_workflow, onRepairAction() {},
  }));
  assert.doesNotMatch(active, /Generate Maintenance Report/);
  assert.equal((active.match(/>Done — Next Step<\/button>/g) || []).length, 1);

  const awaitingResult = { ...stepResponse(3), message_id: 'awaiting-result',
    repair_workflow: { ...workflow, status: 'awaiting_resolution', current_step: null } };
  assert.doesNotMatch(renderToStaticMarkup(createElement(ResponseRenderer, { response: awaitingResult })), /Generate Maintenance Report/);

  for (const status of ['resolved', 'unresolved']) {
    const finalResponse = { ...awaitingResult, message_id: 'final-result',
      repair_workflow: { ...awaitingResult.repair_workflow, status } };
    const finished = renderToStaticMarkup(createElement(ChatThread, {
      messages: [...messages, { id: 'final-result', role: 'assistant', status: 'done', response: finalResponse }],
      repairWorkflow: finalResponse.repair_workflow, onRepairAction() {},
    }));
    assert.equal((finished.match(/>Generate Maintenance Report<\/button>/g) || []).length, 1);
    assert.ok(finished.lastIndexOf('Generate Maintenance Report') > finished.lastIndexOf('Step 3'));
  }
});

test('small-talk replies do not offer maintenance documentation', () => {
  const html=renderToStaticMarkup(createElement(ResponseRenderer,{response:{
    request_type:'smalltalk',session_id:'session-a',message_id:'answer-smalltalk',
    blocks:[{type:'text',content:"I'm doing well, thanks!"}],
  }}));
  assert.doesNotMatch(html,/Create PDF|Documentation preview/);
});

test('unsupported answers do not offer Create PDF', () => {
  const html=renderToStaticMarkup(createElement(ResponseRenderer,{response:{
    documentable:false, request_type:'information', session_id:'session-a',
    message_id:'answer-unsupported', blocks:[{type:'text',
      content:'The required information is not available in the provided PDF manuals.'}],
  }}));
  assert.doesNotMatch(html,/Create PDF|Documentation preview/);
});

test('manual sources are concise, collapsed, and deduplicated', () => {
  const html=renderToStaticMarkup(createElement(ResponseRenderer,{response:{
    blocks:[{type:'image',src:'/images/one',caption:'Approved manual diagram',
      description:'Manual context: Machine HAB-3232 Image ID p1_image_25',
      source:{document_name:'Installation.pdf',page_number:1}},
      {type:'text',content:'### Sources\n- Installation.pdf, PDF page 1; candidate section: Internal'}],
    sources:[
      {document_name:'Installation.pdf',page_number:1},
      {document_name:'Installation.pdf',page_number:1},
    ],
  }}));
  assert.equal((html.match(/Installation\.pdf · page 1/g)||[]).length,1);
  assert.match(html,/<summary>Source<\/summary>/);
  assert.doesNotMatch(html,/Manual context|Image ID|candidate section|<h3>Sources<\/h3>/);
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
  const information = { session_id: 'session-a', message_id: 'information-answer', blocks: [{ type: 'text', content: 'Full component explanation.' },
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
  assert.match(answer, />Create PDF<\/button>/);
  assert.match(answer, />Download PDF<\/button>/);
  const normalThread = renderToStaticMarkup(createElement(ChatThread, {
    repairWorkflow: null, onRepairAction() {}, messages: [
      { id: 'information-only', role: 'assistant', status: 'done', response: information },
    ],
  }));
  assert.ok(normalThread.includes('Full component explanation.'));
  assert.match(normalThread, />Create PDF<\/button>/);
  assert.match(normalThread, />Download PDF<\/button>/);
  assert.doesNotMatch(normalThread, /repair-safety|Maintenance report/);
});
