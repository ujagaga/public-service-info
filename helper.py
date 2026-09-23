import logging
import random
import smtplib
import string
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

import appsettings

logger = logging.getLogger(__name__)


def generate_token():
    return ''.join(random.choices(string.ascii_letters, k=32))


def fetch_html(url: str, timeout: float = 10.0) -> str:
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.text


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


def send_email(recipient, subject, body):
    msg = MIMEMultipart()
    msg['From'] = appsettings.SMTP_USER
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    logger.info(f"Sending email to: {recipient}")
    with smtplib.SMTP(appsettings.SMTP_SERVER, appsettings.SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(appsettings.SMTP_USER, appsettings.SMTP_PASS)
        server.sendmail(appsettings.SMTP_USER, recipient, msg.as_string())
    logger.info("Sending email done")
