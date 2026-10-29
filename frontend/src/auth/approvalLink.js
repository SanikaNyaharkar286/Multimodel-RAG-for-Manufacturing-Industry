export function approvalPortalState(search = '', hash = '') {
  const parameters = new URLSearchParams(search);
  return { open: hash === '#admin' || ['approvals', 'reports', 'notifications', 'dashboard'].includes(parameters.get('admin')), requestUid: parameters.get('request') || '' };
}
