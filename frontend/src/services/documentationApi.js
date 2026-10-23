import { firebaseAuth } from '../auth/firebase';
import { APP_CONFIG } from '../config/appConfig';

export async function documentRequest(path, body, blob = false) {
  const user = firebaseAuth?.currentUser;
  const token = user ? await user.getIdToken() : null;
  const response = await fetch(`${APP_CONFIG.apiBaseUrl}${path}`, {
    method: body ? 'POST' : 'GET',
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not load the report. Please retry.');
  }
  return blob ? response.blob() : response.json();
}

export async function downloadDocument(report) {
  const blob = await documentRequest(`/reports/${report.report_id}/pdf`, null, true);
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = `${report.report_number}.pdf`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
