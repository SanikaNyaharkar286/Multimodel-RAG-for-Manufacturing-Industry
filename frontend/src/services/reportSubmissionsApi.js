import { documentRequest } from './documentationApi';

export const submitReport = report => documentRequest(`/reports/${encodeURIComponent(report.report_id)}/submit`, { revision: report.revision });
export const myReports = (page = 1) => documentRequest(`/reports/my?page=${page}&page_size=20`);
export const adminReports = ({ page = 1, status = '', search = '', sort = 'desc' } = {}) =>
  documentRequest(`/admin/reports?${new URLSearchParams({ page, status, search, sort, page_size: 20 })}`);
export const adminReport = id => documentRequest(`/admin/reports/${encodeURIComponent(id)}`);
export const reviewReport = (id, edit) => documentRequest(`/admin/reports/${encodeURIComponent(id)}/review`, edit, false, 'PATCH');
export const adminNotifications = (page = 1) => documentRequest(`/admin/notifications?page=${page}&page_size=20`);
export const markNotificationRead = id => documentRequest(`/admin/notifications/${encodeURIComponent(id)}/read`, {}, false, 'PATCH');
export const markAllNotificationsRead = () => documentRequest('/admin/notifications/mark-all-read', {});
