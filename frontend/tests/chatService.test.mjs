import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test } from 'node:test';

// Load the HTTP service without Vite; supply only its runtime configuration.
const source = (await readFile(new URL('../src/services/chatService.js', import.meta.url), 'utf8'))
  .replace('import { APP_CONFIG } from "../config/appConfig";',
    'const APP_CONFIG = {apiBaseUrl: "http://localhost:8000", chatEndpoint: "/chat"};')
  .replace("import { logPerformance } from '../utils/performanceLog';", 'const logPerformance = () => {};');
const service = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

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
