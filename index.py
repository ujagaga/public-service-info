#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pip install flask flask-wtf httpx requests
"""

import base64
import datetime
import hmac
import logging
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

from flask import (Flask, g, render_template, request, flash, redirect, make_response,
                   session, url_for as flask_url_for)
from flask_wtf import CSRFProtect

import appsettings
import database
import helper
from oauth import ManualGoogleOAuth
from log_setup import configure_logging

configure_logging()

sys.path.insert(0, os.path.dirname(__file__))

# Runs on every startup path, including CGI where index.py is imported, not executed.
database.setup_initial_db()

application = Flask(__name__, static_url_path='/static', static_folder='static')
application.config['SECRET_KEY'] = appsettings.APP_SECRET_KEY
application.config['SESSION_COOKIE_NAME'] = 'public_service_info'
application.config['WTF_CSRF_SECRET_KEY'] = application.config['SECRET_KEY']
application.config['APPLICATION_ROOT'] = '/'

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
)
logger = logging.getLogger(__name__)

@application.context_processor
def address_settings():
    return {'address_city': getattr(appsettings, 'CITY', 'Novi Sad')}


csrf = CSRFProtect(application)


CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_secret.json")

if application.debug:
    google = None
else:
    google = ManualGoogleOAuth(CLIENT_SECRETS_FILE)


'''
On a CGI hosting, the flasks url_for populates the url with script path,
so you get junk data that does not resolve to a valid url. This is an override to clean it up.
'''
def safe_url_for(endpoint, **values):
    url = flask_url_for(endpoint, **values)
    script_name = request.environ.get('SCRIPT_NAME', '')
    if script_name and url.startswith(script_name):
        url = url[len(script_name):] or '/'
    return url


def current_user():
    token = request.cookies.get('token')
    if not token:
        return None
    user = database.get_user(connection=g.db, token=token)
    return user if user and user["authorized"] > 0 else None


def set_login_cookie(response, token):
    response.set_cookie('token', token, max_age=appsettings.MAX_COOKIE_AGE,
                        expires=time.time() + appsettings.MAX_COOKIE_AGE,
                        httponly=True, secure=request.is_secure, samesite='Lax')


def today():
    return datetime.datetime.now(ZoneInfo("Europe/Belgrade")).date()


def approve_and_notify(email, approval_token=None):
    if not database.approve_user(g.db, email, today(), approval_token):
        return False
    try:
        helper.send_email(
            recipient=email,
            subject=f"{appsettings.APP_TITLE}: registracija odobrena",
            body=f"Vasa registracija je odobrena. Mozete se prijaviti na {request.host_url}",
        )
    except Exception:
        logger.exception("Could not notify approved user %s", email)
        flash("Nalog je odobren, ali mejl potvrde nije poslat. Korisnik može da se prijavi.")
    return True


@application.before_request
def before_request():
    g.db = database.open_db()


@application.teardown_request
def teardown_request(exception):
    if hasattr(g, 'db'):
        database.close_db(g.db)


@application.route('/authorize')
def authorize():
    auth_url = safe_url_for('oauth2callback')
    return google.authorize_redirect(f"https://{request.host}{auth_url}")


@application.route('/login', methods=['GET'])
def login():
    if application.debug:
        token = helper.generate_token()
        user_email = "local@localhost"
        if not database.get_user(connection=g.db, email=user_email):
            database.add_user(connection=g.db, email=user_email, token=token)
        database.update_user(connection=g.db, email=user_email, token=token, authorized=2)

        response = make_response(redirect(safe_url_for('index')))
        set_login_cookie(response, token)
        return response

    return render_template('signin.html', title=appsettings.APP_TITLE, url_for=safe_url_for)


@application.route('/logout')
def logout():
    token = request.cookies.get('token')
    if token:
        database.revoke_token(g.db, token)
    session.clear()
    response = redirect(safe_url_for('login'))
    response.set_cookie('token', 'None', expires=0)
    return response


@application.route('/oauth2callback')
def oauth2callback():
    try:
        google.authorize_access_token()
        user_info = google.get('userinfo').json()
        email = user_info["email"]
        if (not isinstance(email, str) or not email.strip()
                or user_info.get('email_verified') is not True):
            raise ValueError("Google did not return a verified email")
        picture = user_info.get("picture")
    except Exception as error:
        logger.exception(f"OAuth2 callback error {error}")
        flash("Prijava nije uspela. Pokusajte ponovo.")
        response = redirect(safe_url_for("login"))
        response.set_cookie('token', 'None', expires=0)
        return response

    user = database.get_user(connection=g.db, email=email)
    if not user:
        register(email, picture)
        flash("Registracija je poslata. Dobicete mejl kada je administrator odobri.")
        response = redirect(safe_url_for("login"))
        response.set_cookie('token', 'None', expires=0)
        return response

    if user["authorized"] > 0:
        token = helper.generate_token()
        database.update_user(connection=g.db, email=email, token=token, picture=picture)
        response = make_response(redirect(safe_url_for('index')))
        set_login_cookie(response, token)
    else:
        flash("Vas nalog jos uvek nije odobren.")
        response = redirect(safe_url_for("login"))
        response.set_cookie('token', 'None', expires=0)

    return response


def billing(user):
    """Subscription status for regular users; admins are not billed."""
    if user["authorized"] != 1:
        return None
    if not user["subscribed_on"]:
        return {'start': None}
    paid = len(database.get_payments(g.db).get(user["email"], []))
    return {'start': datetime.date.fromisoformat(user["subscribed_on"]),
            'next_due': database.next_due(user["subscribed_on"], paid),
            'missed': database.missed_payments(user["subscribed_on"], paid, today())}


@application.route('/', methods=['GET'])
def index():
    user = current_user()
    if not user:
        return redirect(safe_url_for('login'))
    if user["authorized"] > 1:
        return redirect(safe_url_for('manage_users'))

    resp = make_response(render_template(
        'home.html',
        user=user,
        admin=user["authorized"] > 1,
        subscriptions=database.get_subscriptions(g.db, user["email"]),
        billing=billing(user),
        title=appsettings.APP_TITLE,
        url_for=safe_url_for
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


MAX_LOGO_BYTES = 500 * 1024


@application.route('/', methods=['POST'])
def index_post():
    user = current_user()
    if not user:
        return redirect(safe_url_for('login'))

    if request.form.get('action') == 'logo_delete':
        database.set_logo(g.db, user["email"], None)
        flash("Logo je uklonjen.")
        return redirect(safe_url_for('index'))

    upload = request.files.get('logo')
    if upload and upload.filename:
        data = upload.read(MAX_LOGO_BYTES + 1)
        subtype = helper.image_subtype(data)
        if not subtype:
            flash("Logo mora biti PNG ili JPG slika.")
        elif len(data) > MAX_LOGO_BYTES:
            flash("Logo može imati najviše 500 KB.")
        else:
            database.set_logo(g.db, user["email"],
                              f"data:image/{subtype};base64,{base64.b64encode(data).decode()}")
            flash("Logo je sacuvan.")
        return redirect(safe_url_for('index'))

    if request.form.get('action') == 'signature':
        database.update_user(g.db, user["email"], signature=request.form.get('signature', '').strip())
        flash("Potpis je sacuvan.")
        return redirect(safe_url_for('index'))

    address = request.form.get('address', '').strip()
    if not address:
        return redirect(safe_url_for('index'))

    if request.form.get('action') == 'remove':
        database.set_subscription(g.db, user["email"], address, [])
        flash("Adresa je uklonjena.")
        return redirect(safe_url_for('index'))

    emails = list(dict.fromkeys(e for e in re.split(r'[\s,;]+', request.form.get('emails', '')) if e))
    invalid = [e for e in emails if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', e)]
    if invalid or not emails:
        flash("Unesite bar jednu ispravnu email adresu." if not invalid
              else f"Neispravne email adrese: {', '.join(invalid)}")
        return redirect(safe_url_for('index'))

    database.set_subscription(g.db, user["email"], address, emails)
    flash("Adresa je sacuvana.")
    return redirect(safe_url_for('index'))


def register(email, picture):
    approval_token = helper.generate_token()
    if not database.add_user(connection=g.db, email=email, token=None,
                             approval_token=approval_token, picture=picture):
        return

    approve_link = request.host_url.rstrip('/') + safe_url_for('approve_user', email=email, token=approval_token)
    body = (
        f"Novi korisnik: {email}\n\n"
        f"Odobri: {approve_link}\n"
        f"Ili upravljaj ovde: {request.host_url.rstrip('/')}{safe_url_for('manage_users')}"
    )
    for admin in database.get_user(connection=g.db, authorized=2):
        try:
            helper.send_email(recipient=admin["email"],
                              subject=f"Nova registracija na {appsettings.APP_TITLE}",
                              body=body)
        except Exception as error:
            logger.exception(f"Could not notify admin {admin['email']}: {error}")


@application.route('/approve_user', methods=['GET'])
def approve_user():
    email = request.args.get('email')
    token = request.args.get('token')
    if not email or not token:
        return "Invalid request.", 400

    if not approve_and_notify(email, token):
        return "Invalid or expired approval token.", 403
    return f"Korisnik {email} je odobren.", 200, {"Content-Type": "text/plain; charset=utf-8"}


@application.route('/unsubscribe', methods=['GET', 'POST'])
def unsubscribe():
    email = request.values.get('email', '')
    address = request.values.get('address', '')
    token = request.values.get('token', '')
    if not hmac.compare_digest(token, helper.unsubscribe_token(email, address)):
        return "Invalid unsubscribe link.", 403

    # GET only confirms, so link scanners in mail clients cannot unsubscribe anyone.
    done = request.method == 'POST'
    if done:
        database.unsubscribe(g.db, email, address)
    return render_template('unsubscribe.html', email=email, address=address, token=token, done=done,
                           title=appsettings.APP_TITLE, url_for=safe_url_for)


@application.route('/manage_users', methods=['GET'])
def manage_users():
    user = current_user()
    if not user:
        return redirect(safe_url_for('login'))
    if user["authorized"] < 2:
        flash("Nemate ovlascenje za upravljanje korisnicima.")
        return redirect(safe_url_for('index'))

    unauthorized_users = sorted(
        database.get_user(connection=g.db, authorized=0),
        key=lambda u: u["email"].lower()
    )
    authorized_users = sorted(
        (u for u in database.get_user(connection=g.db, authorized=1)
         + database.get_user(connection=g.db, authorized=2)
         if u["email"].lower() != user["email"].lower()),
        key=lambda u: u["email"].lower()
    )

    payments = database.get_payments(g.db)
    resp = make_response(render_template('manage_users.html', user=user,
                                         unauthorized_users=unauthorized_users,
                                         authorized_users=authorized_users,
                                         subscriptions=database.get_subscriptions(g.db),
                                         payments=payments, today=today(), next_due=database.next_due,
                                         missed_payments=database.missed_payments,
                                         title=appsettings.APP_TITLE, url_for=safe_url_for))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@application.route('/manage_users', methods=['POST'])
def manage_users_post():
    user = current_user()
    if not user or user["authorized"] < 2:
        flash("Nemate ovlascenje za ovu akciju.")
        return redirect(safe_url_for('index'))

    email = request.form.get('email')
    action = request.form.get('action')
    if not email or not action:
        return redirect(safe_url_for('manage_users'))

    if action == 'authorize':
        approve_and_notify(email)
    elif action == 'make_admin':
        target = database.get_user(connection=g.db, email=email)
        if target and target['authorized'] == 1:
            database.update_user(connection=g.db, email=email, authorized=2)
    elif action == 'remove':
        database.delete_user(connection=g.db, email=email)
    elif action == 'set_subscribed_on':
        try:
            database.set_subscribed_on(g.db, email, parse_date(request.form.get('subscribed_on', '')))
        except ValueError:
            flash("Neispravan datum pretplate.")
    elif action in ('add_payment', 'update_payment'):
        try:
            amount = float(request.form.get('amount', '').replace(',', '.'))
            paid_on = parse_date(request.form.get('paid_on', ''))
        except ValueError:
            amount = 0
        if amount <= 0:
            flash("Unesite iznos i datum uplate.")
        elif action == 'add_payment':
            database.add_payment(g.db, email, amount, paid_on)
        else:
            database.update_payment(g.db, request.form.get('payment_id', type=int), email, amount, paid_on)
    elif action == 'delete_payment':
        database.delete_payment(g.db, request.form.get('payment_id', type=int), email)

    return redirect(safe_url_for('manage_users'))


def parse_date(value):
    """Admin forms always use dd.mm.yyyy, independent of browser locale."""
    return datetime.datetime.strptime(value.strip(), '%d.%m.%Y').date()


def send_payment_reminders():
    dues = database.unreminded_dues(g.db, today())
    if not dues:
        return
    body = ("Dospele uplate pretplate:\n\n"
            + "\n".join(f"{email}: {due:%d.%m.%Y}" for email, due in dues)
            + f"\n\nUpravljaj ovde: {request.host_url.rstrip('/')}{safe_url_for('manage_users')}")
    sent = False
    for admin in database.get_user(connection=g.db, authorized=2):
        try:
            helper.send_email(recipient=admin["email"], subject=f"{appsettings.APP_TITLE}: dospele uplate", body=body)
            sent = True
        except Exception as error:
            logger.exception(f"Could not send payment reminder to {admin['email']}: {error}")
    # Unsent reminders are retried on the next /run_check call.
    if sent:
        database.record_reminders(g.db, dues)


@application.route('/run_check', methods=['GET'])
@csrf.exempt
def run_check():
    if request.args.get('token') != appsettings.TRIGGER_TOKEN:
        return "Forbidden", 403

    # Independent of outage checks: a failure here must not block them, and vice versa.
    try:
        send_payment_reminders()
    except Exception as error:
        logger.exception(f"Payment reminders failed: {error}")

    try:
        # Imported here so the Gemini client is only created when a check actually runs.
        import checker

        result = checker.run_checks(request.host_url.rstrip('/') + safe_url_for('unsubscribe'))
    except Exception as error:
        logger.exception(f"Check run failed: {error}")
        return f"Check run failed: {error}", 500

    if result["skipped"]:
        return f"Provera za {result['date']} je vec uspesno obavljena danas. Nema mejlova za ponovno slanje.\n", 200, \
               {"Content-Type": "text/plain; charset=utf-8"}

    if result["errors"]:
        logger.error(f"Check run errors: {result['errors']}")

    return (
        f"Datum: {result['date']}\n"
        f"Provera: {'sacuvana ranije' if result.get('check_cached') else 'obavljena u ovom pozivu'}\n"
        f"Provera zavrsena: {'da' if result.get('check_complete') else 'ne'}\n"
        f"Provereno korisnika: {result['checked']}\n"
        f"Obavesteno: {', '.join(result['notified']) or '-'}\n"
        f"Preostalih mejlova za slanje: {result.get('pending', 0)}\n"
        f"Greske: {'; '.join(result['errors']) or '-'}\n"
    ), 500 if result["errors"] else 200, {"Content-Type": "text/plain; charset=utf-8"}

if __name__ == "__main__":
    application.run(debug=False, host="0.0.0.0", port=5000)
