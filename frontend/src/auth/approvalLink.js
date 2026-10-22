export function approvalPortalState(search = '', hash = '') {
  const parameters = new URLSearchParams(search);
  return { open: hash === '#admin' || parameters.get('admin') === 'approvals', requestUid: parameters.get('request') || '' };
}
