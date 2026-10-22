import assert from 'node:assert/strict';
import { test } from 'node:test';
import { detectSafetyGuidance } from '../src/config/safetyGuidance.js';

test('safety guidance detects physical repair intent and categories locally', () => {
  const guidance = detectSafetyGuidance('The motor is overheating. What should I inspect?');
  assert.equal(guidance.title, 'Safety precautions');
  assert.ok(guidance.bullets.some(text => text.includes('cool')));
});

test('informational and navigation messages do not trigger safety guidance', () => {
  assert.equal(detectSafetyGuidance('Hello.'), null);
  assert.equal(detectSafetyGuidance('What is this machine used for?'), null);
  assert.equal(detectSafetyGuidance('Done.', { activeRepair: true, navigation: true }), null);
  assert.equal(detectSafetyGuidance('Explain the function of the spindle.', { activeRepair: true }), null);
});

test('repair continuation can use active task context', () => {
  assert.ok(detectSafetyGuidance('The issue is still present.', { activeRepair: true }));
});
