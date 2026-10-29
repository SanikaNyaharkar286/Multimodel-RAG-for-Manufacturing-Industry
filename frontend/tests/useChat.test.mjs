import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test } from 'node:test';

test('expired backend sessions are forgotten without replaying a repair confirmation', async () => {
  const slots = [];
  let cursor = 0;
  globalThis.__chatHookHarness = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => {
        slots[index] = typeof value === 'function' ? value(slots[index]) : value;
      }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = { current: initial };
      return slots[index];
    },
    // Exercise request handling directly without mounting a browser or timers.
    useEffect() {},
  };
  let opened = 0;
  const sent = [];
  globalThis.__chatHookServices = {
    cancelChatRequest() {},
    closeChatSession: async () => {},
    openChatSession: async () => ({ session_id: `session-${++opened}`,
      expires_at: Date.now() / 1000 + 3600, messages: [] }),
    sendChat: async request => {
      sent.push(request);
      if (sent.length === 1) {
        const error = new Error('Session expired or server restarted. Start a new chat.');
        error.status = 410;
        throw error;
      }
      return { blocks: [{ type: 'text', content: 'New response' }] };
    },
  };
  try {
    const source = (await readFile(new URL('../src/hooks/useChat.js', import.meta.url), 'utf8'))
      .replace(/import\s*\{[\s\S]*?\}\s*from "react";/,
        'const {useState, useEffect, useRef} = globalThis.__chatHookHarness;')
      .replace(/import\s*\{[\s\S]*?\}\s*from "\.\.\/services\/chatService";/,
        'const {cancelChatRequest, closeChatSession, openChatSession, sendChat} = globalThis.__chatHookServices;')
      .replace('import { validateImage } from "../utils/validateImage";',
        'const validateImage = () => ({valid: true});')
      .replace('import { detectSafetyGuidance } from "../config/safetyGuidance";',
        'const detectSafetyGuidance = () => null;')
      .replace("import { logPerformance } from '../utils/performanceLog';",
        'const logPerformance = () => {};')
      .replace("import { preloadStepImages, clearManualImageCache } from '../services/manualImageCache';",
        'const preloadStepImages = () => {}; const clearManualImageCache = () => {};');
    const { default: useChat } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
    function HookHarness() { return useChat(); }
    const render = () => { cursor = 0; return HookHarness(); };
    let chat = render();
    await chat.sendRepairAction({ action: 'done', workflow_id: 'old-repair', step_id: 'old-step' });
    assert.equal(opened, 1);
    assert.equal(sent.length, 1, 'A failed Done must never be automatically retried');
    chat = render();
    assert.equal(chat.repairWorkflow, null);
    chat.setQuestion('Explain the new machine');
    chat = render();
    await chat.sendMessage();
    assert.equal(opened, 2, 'A new question must open a fresh session after HTTP 410');
    assert.equal(sent[1].sessionId, 'session-2');
    assert.equal(sent[1].repairAction, null);
  } finally {
    delete globalThis.__chatHookHarness;
    delete globalThis.__chatHookServices;
  }
});

test('Done shows a cached step before network confirmation and rolls back a rejected transition', async () => {
  const slots = [];
  let cursor = 0;
  globalThis.__fastStepHarness = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = {current: initial};
      return slots[index];
    },
    useEffect() {},
  };
  const workflow = number => ({workflow_id: 'case', plan_version: 1, status: 'awaiting_done',
    current_step: {step_id: `step-${number}`, number}, completed_steps: []});
  const reply = number => ({repair_workflow: workflow(number), blocks: [{type: 'text', content: `Prepared step ${number}`}],
    next_step_previews: [{workflow_id: 'case', step_id: `step-${number}`, plan_version: 1, observation: '',
      response: {repair_workflow: workflow(number + 1), blocks: [{type: 'text', content: `Prepared step ${number + 1}`}]}}]});
  let calls = 0;
  let confirm;
  let reject;
  globalThis.__fastStepServices = {
    cancelChatRequest() {}, closeChatSession: async () => {},
    openChatSession: async () => ({session_id: 's', expires_at: Date.now() / 1000 + 3600, messages: []}),
    sendChat: async () => {
      calls++;
      if (calls === 1) return reply(1);
      return new Promise((resolve, fail) => { confirm = resolve; reject = fail; });
    },
  };
  try {
    const source = (await readFile(new URL('../src/hooks/useChat.js', import.meta.url), 'utf8'))
      .replace(/import\s*\{[\s\S]*?\}\s*from "react";/,
        'const {useState, useEffect, useRef} = globalThis.__fastStepHarness;')
      .replace(/import\s*\{[\s\S]*?\}\s*from "\.\.\/services\/chatService";/,
        'const {cancelChatRequest, closeChatSession, openChatSession, sendChat} = globalThis.__fastStepServices;')
      .replace('import { validateImage } from "../utils/validateImage";', 'const validateImage = () => ({valid:true});')
      .replace("import { logPerformance } from '../utils/performanceLog';", 'const logPerformance = () => {};')
      .replace("import { preloadStepImages, clearManualImageCache } from '../services/manualImageCache';",
        'const preloadStepImages = () => {}; const clearManualImageCache = () => {};');
    const {default: useChat} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
    const render = () => { cursor = 0; return useChat(); };
    let chat = render();
    chat.setQuestion('Help with the machine fault');
    chat = render();
    await chat.sendMessage();
    chat = render();
    const saving = chat.sendRepairAction({action: 'done', workflow_id: 'case', step_id: 'step-1', safety_confirmed: true});
    chat = render();
    assert.equal(chat.messages.at(-1).status, 'done');
    assert.equal(chat.messages.at(-1).response.blocks[0].content, 'Prepared step 2');
    assert.equal(chat.messages.at(-1).awaitingConfirmation, true);
    assert.equal(chat.loading, true, 'Further actions wait for authoritative confirmation');
    await Promise.resolve();
    confirm(reply(2));
    await saving;
    chat = render();
    assert.equal(chat.messages.at(-1).awaitingConfirmation, false);
    const failed = chat.sendRepairAction({action: 'done', workflow_id: 'case', step_id: 'step-2'});
    chat = render();
    assert.equal(chat.messages.at(-1).response.blocks[0].content, 'Prepared step 3');
    await Promise.resolve();
    reject(Object.assign(new Error('Stale step'), {status: 409}));
    await failed;
    chat = render();
    assert.equal(chat.repairWorkflow.current_step.step_id, 'step-2');
    assert.equal(chat.messages.at(-1).status, 'error');
    assert.equal(chat.messages.at(-1).response, null);
  } finally {
    delete globalThis.__fastStepHarness;
    delete globalThis.__fastStepServices;
  }
});
