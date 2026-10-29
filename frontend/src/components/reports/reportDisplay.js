export const reportDate = value => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString() : 'Not recorded';
export const reviewLabel = status => ({ new: 'New', under_review: 'Under Review', reviewed: 'Reviewed' }[status] || 'Not recorded');
export const emailLabel = submission => ({ pending: 'Email queued', sending: 'Email sending',
  sent: 'Email accepted by mail server; inbox delivery is not confirmed', failed: 'Email notification needs attention' }[submission?.email_status] || 'Email status unavailable');
