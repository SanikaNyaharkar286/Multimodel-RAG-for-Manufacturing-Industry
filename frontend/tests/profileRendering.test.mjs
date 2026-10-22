import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom',
  customLogger: { info() {}, warn() {}, warnOnce() {}, error() {}, clearScreen() {}, hasErrorLogged() { return false; }, hasWarned: false } });
after(() => server.close());
const { default: UserProfilePage } = await server.ssrLoadModule('/src/components/UserProfilePage.jsx');

test('profile page renders safe current-user fields and approved badge', () => {
  const html = renderToStaticMarkup(createElement(UserProfilePage, { onBack() {}, onLogout() {}, profile: {
    name: 'Rajvardhan Jadhav', email: 'user@example.com', phone: '1234567890',
    employee_id: 'EMP001', company_name: 'Example Manufacturing',
    role: 'technician', status: 'approved',
  } }));
  for (const value of ['My Profile', 'Rajvardhan Jadhav', 'user@example.com',
    'EMP001', 'Example Manufacturing', 'Technician', 'Approved']) assert.ok(html.includes(value));
  assert.ok(!html.includes('1234567890'));
  assert.match(html, /profile-status--approved/);
});

test('optional fields are hidden and pending status is clear', () => {
  const html = renderToStaticMarkup(createElement(UserProfilePage, { onBack() {}, onLogout() {}, profile: {
    name: 'Pending User', email: 'pending@example.com', role: 'technician', status: 'pending',
  } }));
  assert.match(html, /profile-status--pending/);
  assert.doesNotMatch(html, /Employee ID|Company name/);
});

test('loading and error states use safe user messages', () => {
  const loading = renderToStaticMarkup(createElement(UserProfilePage, { onBack() {}, onLogout() {}, loading: true }));
  const error = renderToStaticMarkup(createElement(UserProfilePage, { onBack() {}, onLogout() {}, error: true }));
  assert.ok(loading.includes('Loading profile…'));
  assert.ok(error.includes('Unable to load your profile. Please refresh or sign in again.'));
  assert.doesNotMatch(error, /Firebase|token|Firestore/);
});
