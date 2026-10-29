# Sends an email to the admin when a new technician registers and requests access to the Manufacturing Copilot.

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
import logging
import os
import re
import smtplib
import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app import config as cfg

logger = logging.getLogger('uvicorn.error.approval_email')


# holds all the email settings in one place. the password is hidden when printed
@dataclass(frozen=True)
class EmailSettings:
    recipient: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    sender: str
    sender_name: str
    use_tls: bool
    portal_url: str


# reads the email settings from .env and checks them.
# if anything is missing or wrong, it logs a warning and returns None so no email is sent
def load_settings(recipient=None):
    # app.config already loads backend/.env and won't replace real server values
    recipient = recipient if recipient is not None else os.getenv('ADMIN_NOTIFICATION_EMAIL', '').strip()
    if not recipient:
        logger.warning('Admin email notification skipped: ADMIN_NOTIFICATION_EMAIL is missing')
        return None
    values = {name: os.getenv(name, '').strip() for name in
              ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM_EMAIL')}
    missing = [name for name, value in values.items() if not value]
    if missing:
        logger.warning('Admin email notification skipped: missing settings %s', ', '.join(missing))
        return None
    try:
        port = int(os.getenv('SMTP_PORT', '587'))
        if not 1 <= port <= 65535:
            raise ValueError('Invalid port')
        tls = os.getenv('SMTP_USE_TLS', 'true').strip().lower()
        if tls not in {'true', 'false'}:
            raise ValueError('Invalid TLS setting')
        for address in (recipient, values['SMTP_FROM_EMAIL']):
            if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', address):
                raise ValueError('Invalid address')
        sender_name = os.getenv('SMTP_FROM_NAME', 'Manufacturing Equipment Copilot').strip()
        if '\r' in sender_name or '\n' in sender_name:
            raise ValueError('Invalid sender name')
        portal = os.getenv('APPROVAL_PORTAL_URL', cfg.ORIGIN).strip()
        url = urlsplit(portal)
        if url.scheme not in {'http', 'https'} or not url.netloc or url.username or url.password:
            raise ValueError('Invalid dashboard URL')
    except ValueError:
        logger.warning('Admin email notification skipped: invalid SMTP or dashboard configuration')
        return None
    return EmailSettings(recipient, values['SMTP_HOST'], port, values['SMTP_USERNAME'],
                         values['SMTP_PASSWORD'], values['SMTP_FROM_EMAIL'], sender_name,
                         tls == 'true', portal)


# builds the email the admin will see, with the technician's details
# and a link to the approval dashboard
def build_approval_message(profile, settings):
    url = urlsplit(settings.portal_url)
    query = {key: value for key, value in parse_qsl(url.query) if key not in {'admin', 'request'}}
    query.update(admin='approvals', request=profile['uid'])
    link = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ''))
    requested = profile.get('createdAt')
    if isinstance(requested, datetime):
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        requested = requested.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    else:
        requested = 'Not recorded'
    company = cfg.COMPANY_NAME
    if not company or company == 'COMPANY_NAME':
        company = profile.get('companyId') or 'Not provided'
    fields = [('Name', profile.get('fullName')), ('Email', profile.get('email')),
              ('Company', company), ('Employee ID', profile.get('employeeId')),
              ('Phone', profile.get('phone')), ('Department', profile.get('department')),
              ('Site', profile.get('site')), ('Designation', profile.get('designation')),
              ('Reason for access', profile.get('requestReason')), ('Requested at', requested)]
    message = EmailMessage()
    message['Subject'] = 'New Technician Approval Request'
    message['From'] = formataddr((settings.sender_name, settings.sender))
    message['To'] = settings.recipient
    message.set_content(
        'A new technician has requested access to Manufacturing Equipment Copilot.\n\n'
        + '\n'.join(f'{label}: {value or "Not provided"}' for label, value in fields)
        + '\n\nStatus: Pending approval\n\n'
        'Please open the admin approval dashboard to approve or reject this request.\n'
        f'{link}\n\nSign in as an administrator. Opening this link does not approve access.\n')
    return message


# sends the approval email to the admin. gives back True if sent, False if anything fails
def send_admin_approval_notification(profile):
    """returns False on any error. never undo the pending profile because of an email problem."""
    try:
        settings = load_settings()
        if settings is None:
            return False
        message = build_approval_message(profile, settings)
        with smtplib.SMTP(settings.host, settings.port, timeout=10) as smtp:
            smtp.ehlo()
            if settings.use_tls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(settings.username, settings.password)
            refused = smtp.send_message(message, from_addr=settings.sender,
                                        to_addrs=[settings.recipient])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        logger.info('Admin email notification sent successfully uid=%s', profile['uid'])
        return True
    except Exception as exc:
        code = getattr(exc, 'smtp_code', None)
        # only log the error type and code, never the smtp reply or any password
        logger.error('Admin email notification failed uid=%s error_type=%s smtp_code=%s',
                     profile.get('uid', 'unknown'), type(exc).__name__,
                     code if isinstance(code, int) else None)
        return False
