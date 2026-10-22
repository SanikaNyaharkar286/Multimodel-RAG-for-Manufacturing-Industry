import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test } from 'node:test';

// Load the HTTP service without Vite; supply only its runtime configuration.
const source = (await readFile(new URL('../src/services/chatService.js', import.meta.url), 'utf8'))
  .replace('import { APP_CONFIG } from "../config/appConfig";',
    'const APP_CONFIG = {apiBaseUrl: "http://localhost:8000", chatEndpoint: "/chat"};')
  .replace("import { logPerformance } from '../utils/performanceLog';", 'const logPerformance = () => {};')
  .replace("import { firebaseAuth } from '../auth/firebase';", 'const firebaseAuth = null;');
const service = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('repair confirmations send the exact workflow and step with report fields', async (t) => {
  let form;
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    form = options.body;
    return { ok: true, json: async () => ({ blocks: [] }) };
  });
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { setItem() {} } });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor);
    else delete globalThis.sessionStorage;
  });
  await service.sendChat({ sessionId: 'session', repairAction: {
    action: 'resolve', workflow_id: 'repair-1', step_id: 'step-1', machine: 'Haas VF-2',
    issue: 'Spindle overheating', cause: 'Lubrication blockage', action_taken: 'Cleaned lubrication line',
  } });
  assert.equal(form.get('repair_action'), 'resolve');
  assert.equal(form.get('workflow_id'), 'repair-1');
  assert.equal(form.get('step_id'), 'step-1');
  assert.equal(form.get('cause'), 'Lubrication blockage');
  assert.equal(form.get('action_taken'), 'Cleaned lubrication line');
  assert.equal(form.get('image'), null);
});

test('issue reports send the same workflow and observation fields', async (t) => {
  let form;
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    form = options.body;
    return { ok: true, json: async () => ({ blocks: [] }) };
  });
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { setItem() {} } });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor);
    else delete globalThis.sessionStorage;
  });
  await service.sendChat({ sessionId: 'session', repairAction: {
    action: 'report_issue', workflow_id: 'repair-1', step_id: 'step-1',
    issue_report: 'Pressure is still zero.',
  } });
  assert.equal(form.get('repair_action'), 'report_issue');
  assert.equal(form.get('issue_report'), 'Pressure is still zero.');
});

test('issue reports can include the technician image', async (t) => {
  let form;
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    form = options.body;
    return { ok: true, json: async () => ({ blocks: [] }) };
  });
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { setItem() {} } });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor);
    else delete globalThis.sessionStorage;
  });
  const image = new File(['image bytes'], 'issue.png', { type: 'image/png' });
  await service.sendChat({ sessionId: 'session', imageFile: image, repairAction: {
    action: 'report_issue', workflow_id: 'repair-1', step_id: 'step-1',
    issue_report: 'The issue remains after the inspection.',
  } });
  assert.equal(form.get('image').name, 'issue.png');
  assert.equal(form.get('image').type, 'image/png');
});

test('browser allows the configured backend deadline plus response margin', async (t) => {
  let delay;
  t.mock.method(globalThis, 'setTimeout', (_callback, ms) => { delay = ms; return 1; });
  t.mock.method(globalThis, 'clearTimeout', () => {});
  t.mock.method(globalThis, 'fetch', async () => ({ok: true, json: async () => ({blocks: []})}));
  const storageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', {configurable: true, value: {setItem() {}}});
  t.after(() => {
    if (storageDescriptor) Object.defineProperty(globalThis, 'sessionStorage', storageDescriptor);
    else delete globalThis.sessionStorage;
  });
  await service.sendChat({question: 'Explain this', sessionId: 's', requestTimeoutSeconds: 500});
  assert.equal(delay, 515000);
  await service.sendChat({question: 'Explain this', sessionId: 's'});
  assert.equal(delay, 135000);
});

for (const reason of ['timeout', 'cancel']) {
  test(`reports ${reason} distinctly`, async (t) => {
    let expire;
    t.mock.method(globalThis, 'setTimeout', (callback) => { expire = callback; return 1; });
    t.mock.method(globalThis, 'clearTimeout', () => {});
    t.mock.method(globalThis, 'fetch', (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    const pending = service.sendChat({question: 'Explain this', sessionId: 's'});
    if (reason === 'timeout') expire();
    else service.cancelChatRequest();
    await assert.rejects(pending, reason === 'timeout' ? /answer took too long/ : /request was cancelled/);
  });
}

test('correlates response headers with browser timing metadata', async (t) => {
  t.mock.method(globalThis, 'setTimeout', () => 1);
  t.mock.method(globalThis, 'clearTimeout', () => {});
  t.mock.method(globalThis, 'fetch', async () => ({ok: true,
    headers: new Headers({'X-Request-ID': 'request-123', 'Server-Timing': 'agent;dur=25'}),
    json: async () => ({blocks: [{type: 'image', src: '/images/test'}]}),
  }));
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', {configurable: true, value: {setItem() {}}});
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor);
    else delete globalThis.sessionStorage;
  });
  const response = await service.sendChat({question: 'private question', sessionId: 'private-session'});
  assert.equal(response.request_id, 'request-123');
  assert.equal(response.client_timing.server_timing, 'agent;dur=25');
  assert.ok(response.client_timing.fetch_total_ms >= 0);
  assert.equal(response.blocks[0].src, 'http://localhost:8000/images/test');
  assert.ok(!JSON.stringify(response.client_timing).includes('private'));
});
