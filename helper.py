import hashlib
import hmac
import logging
import secrets
import smtplib
import time
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

import appsettings

logger = logging.getLogger(__name__)


def generate_token():
    return secrets.token_urlsafe(32)


def unsubscribe_token(email: str, address: str) -> str:
    message = f"{email}\n{address}".encode()
    return hmac.new(appsettings.APP_SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def fetch_html(url: str, timeout: float = 10.0) -> str:
    started = time.monotonic()
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        html = response.text
    except Exception:
        logger.exception("Data fetch failed: %s", url)
        raise
    logger.info("Data fetch successful: %s (%.2f seconds)", url, time.monotonic() - started)
    return html


def time_ago(last_seen) -> str:
    if not last_seen:
        return "never"
    days = (int(time.time()) - int(last_seen)) // 86400
    if days < 1:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        return f"{days // 30} months ago"
    return "more than a year ago"


def image_subtype(data: bytes) -> str | None:
    """'png' or 'jpeg' judged by file content, not by name or browser-supplied type."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    return None


def send_email(recipient, subject, body, html=None, images=()):
    """images: (content_id, bytes, subtype) referenced from html as cid:content_id."""
    if html:
        msg = MIMEMultipart('related')
        alternative = MIMEMultipart('alternative')
        alternative.attach(MIMEText(body, 'plain'))
        alternative.attach(MIMEText(html, 'html'))
        msg.attach(alternative)
        for content_id, data, subtype in images:
            image = MIMEImage(data, _subtype=subtype)
            image.add_header('Content-ID', f'<{content_id}>')
            image.add_header('Content-Disposition', 'inline')
            msg.attach(image)
    else:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, 'plain'))
    msg['From'] = appsettings.SMTP_USER
    msg['To'] = recipient
    msg['Subject'] = subject

    logger.info(f"Sending email to: {recipient}")
    with smtplib.SMTP(appsettings.SMTP_SERVER, appsettings.SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(appsettings.SMTP_USER, appsettings.SMTP_PASS)
        server.sendmail(appsettings.SMTP_USER, recipient, msg.as_string())
    logger.info("Sending email done")

