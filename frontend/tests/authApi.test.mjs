import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const source = (await readFile(new URL('../src/services/authApi.js', import.meta.url), 'utf8'))
  .replace('import { APP_CONFIG } from "../config/appConfig";',
    'const APP_CONFIG = {apiBaseUrl: "http://localhost:8000"};');
const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('current profile request uses the Firebase token and never sends a user ID', async (t) => {
  let requestedUrl;
  let requestedOptions;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requestedUrl = url;
    requestedOptions = options;
    return { ok: true, json: async () => ({ name: 'Current User', status: 'approved' }) };
  });
  const user = { getIdToken: async () => 'verified-token' };
  const profile = await api.getCurrentProfile(user);
  assert.equal(requestedUrl, 'http://localhost:8000/auth/me');
  assert.equal(requestedOptions.headers.Authorization, 'Bearer verified-token');
  assert.ok(!requestedUrl.includes('uid'));
  assert.equal(profile.name, 'Current User');
});

test('profile API failures expose only a generic client error', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: false, status: 401, json: async () => ({ detail: 'private Firebase provider detail' }),
  }));
  await assert.rejects(api.getCurrentProfile({ getIdToken: async () => 'expired' }),
    error => error.message === 'Authenticated request failed' && error.status === 401
      && !error.message.includes('Firebase'));
});

for (const status of ['approved', 'pending', 'suspended']) {
  test(`${status} account startup uses one profile request`, async (t) => {
    const urls = [];
    t.mock.method(globalThis, 'fetch', async url => {
      urls.push(url);
      return { ok: true, json: async () => ({ name: 'Technician', status }) };
    });
    const profile = await api.getSessionProfile({ emailVerified: true, getIdToken: async () => 'token' });
    assert.equal(profile.status, status);
    assert.deepEqual(urls, ['http://localhost:8000/auth/me']);
  });
}

test('missing profile still uses the registration and administrator setup status', async (t) => {
  const urls = [];
  t.mock.method(globalThis, 'fetch', async url => {
    urls.push(url);
    return url.endsWith('/me')
      ? { ok: false, status: 404, json: async () => ({}) }
      : { ok: true, json: async () => ({ status: 'admin_setup_required' }) };
  });
  const profile = await api.getSessionProfile({ emailVerified: true, getIdToken: async () => 'token' });
  assert.equal(profile.status, 'admin_setup_required');
  assert.deepEqual(urls, ['http://localhost:8000/auth/me', 'http://localhost:8000/auth/status']);
});

test('rejected accounts preserve the administrator reason', async (t) => {
  t.mock.method(globalThis, 'fetch', async url => ({ ok: true, json: async () =>
    url.endsWith('/me') ? { name: 'Technician', status: 'rejected' }
      : { status: 'rejected', decisionReason: 'Contact the administrator.' } }));
  const profile = await api.getSessionProfile({ emailVerified: true, getIdToken: async () => 'token' });
  assert.equal(profile.name, 'Technician');
  assert.equal(profile.decisionReason, 'Contact the administrator.');
});

test('unverified users do not make protected profile requests', async (t) => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => { calls++; });
  assert.deepEqual(await api.getSessionProfile({ emailVerified: false, email: 'tech@example.com' }),
    { status: 'unverified', email: 'tech@example.com' });
  assert.equal(calls, 0);
});
