import { reportDate } from './reportDisplay';

export default function AdminNotifications({ data, error, onOpen, onRead, onReadAll, onRefresh, onPage, page = 1, busy }) {
  return <section className="admin-notifications" aria-label="Admin notifications"><header><h2>Notifications {data ? `(${data.unread_count} unread)` : ''}</h2>
    <button disabled={!data?.unread_count || busy} onClick={onReadAll}>Mark all as read</button></header>
    {error && <p role="alert">{error} {onRefresh && <button disabled={busy} onClick={onRefresh}>Retry notifications</button>}</p>}{!data && !error && <p role="status">Loading notifications…</p>}
    {data?.notifications.map(item => <article key={item.notification_id} className={`admin-notification ${item.is_read ? '' : 'is-unread'}`}>
      <h3>{item.title}</h3><p>{item.message}</p><small>{reportDate(item.created_at)} · {item.is_read ? 'Read' : 'Unread'}</small>
      <div><button disabled={busy} onClick={() => onOpen(item)}>Open</button>{!item.is_read && <button disabled={busy} onClick={() => onRead(item.notification_id)}>Mark as read</button>}</div>
    </article>)}{data && !data.notifications.length && <p>No notifications.</p>}
    <div className="reports-pagination"><button disabled={page === 1 || busy} onClick={() => onPage(page-1)}>Previous</button><span>Page {page}</span><button disabled={data?.notifications.length < 20 || !data || busy || page >= 1000} onClick={() => onPage(page+1)}>Next</button></div>
  </section>;
}
