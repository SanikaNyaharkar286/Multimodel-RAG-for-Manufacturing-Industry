import { firebaseAuth } from '../auth/firebase';
import { APP_CONFIG } from '../config/appConfig';

async function adminRequest(path, options = {}) {
  const user = firebaseAuth?.currentUser;
  const token = user ? await user.getIdToken() : null;
  const response = await fetch(`${APP_CONFIG.apiBaseUrl}${path}`, {
    ...options,
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(options.headers || {}) },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(typeof data.detail === 'string' ? data.detail : 'Could not complete the file request.');
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export const listAdminMachines = () => adminRequest('/admin/files/machines');

export function uploadAdminFiles(machineName, files) {
  const body = new FormData();
  body.append('machine_name', machineName);
  files.forEach(file => body.append('files', file, file.name));
  return adminRequest('/admin/files/upload', { method: 'POST', body });
}
