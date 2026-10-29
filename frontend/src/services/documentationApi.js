import { firebaseAuth } from '../auth/firebase';
import { APP_CONFIG } from '../config/appConfig';

export async function documentRequest(path, body, blob = false, method) {
  const user = firebaseAuth?.currentUser;
  const token = user ? await user.getIdToken() : null;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), blob || body ? 180000 : 30000);
  const request = accessToken => fetch(`${APP_CONFIG.apiBaseUrl}${path}`, {
    method: method || (body ? 'POST' : 'GET'),
    signal: controller.signal,
    headers: { ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  try {
    let response = await request(token);
    if (response.status === 401 && user) response = await request(await user.getIdToken(true));
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const error = new Error(typeof data.detail === 'string' ? data.detail : 'Could not load the report. Please retry.');
      error.status = response.status;
      throw error;
    }
    return await (blob ? response.blob() : response.json());
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('The report request timed out. Reopen the report to check its latest state before retrying.');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export async function reportPdfUrl(reportId, admin = false) {
  const blob = await documentRequest(`${admin ? '/admin' : ''}/reports/${encodeURIComponent(reportId)}/pdf`, null, true);
  return URL.createObjectURL(blob);
}

export async function downloadDocument(report) {
  const blob = await documentRequest(`/reports/${encodeURIComponent(report.report_id)}/pdf`, null, true);
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = `${report.report_number || report.report_id}.pdf`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
