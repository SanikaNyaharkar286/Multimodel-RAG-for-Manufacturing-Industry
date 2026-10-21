export function approvalPortalState(search = '') {
  const parameters = new URLSearchParams(search);
  return { open: parameters.get('admin') === 'approvals', requestUid: parameters.get('request') || '' };
}
