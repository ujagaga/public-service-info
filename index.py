#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pip install flask authlib flask-wtf httpx
"""

import json
import logging
import os
import sys
import time

from authlib.integrations.flask_client import OAuth
from flask import (Flask, g, render_template, request, flash, redirect, make_response,
                   url_for as flask_url_for)
from flask_wtf import CSRFProtect

import appsettings
import database
import helper

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

csrf = CSRFProtect(application)

CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_secret.json")

if application.debug:
    google = None
else:
    with open(CLIENT_SECRETS_FILE) as f:
        client_secrets = json.load(f)['web']

    oauth = OAuth(application)
    google = oauth.register(
        name='google',
        client_id=client_secrets['client_id'],
        client_secret=client_secrets['client_secret'],
        access_token_url=client_secrets['token_uri'],
        access_token_params=None,
        authorize_url=client_secrets['auth_uri'],
        authorize_params=None,
        api_base_url='https://www.googleapis.com/oauth2/v1/',
        userinfo_endpoint='https://www.googleapis.com/oauth2/v3/userinfo',
        client_kwargs={'scope': 'email'},
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration'
    )


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
    return database.get_user(connection=g.db, token=token)


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
            database.add_user(connection=g.db, email=user_email, token=token, address='')
        database.update_user(connection=g.db, email=user_email, token=token, authorized=2)

        response = make_response(redirect(safe_url_for('index')))
        response.set_cookie('token', token, max_age=appsettings.MAX_COOKIE_AGE,
                            expires=time.time() + appsettings.MAX_COOKIE_AGE)
        return response

    return render_template('signin.html', title=appsettings.APP_TITLE, url_for=safe_url_for)


@application.route('/logout')
def logout():
    response = redirect(safe_url_for('login'))
    response.set_cookie('token', 'None', expires=0)
    return response


@application.route('/oauth2callback')
def oauth2callback():
    try:
        google.authorize_access_token()
        user_info = google.get('userinfo').json()
        email = user_info["email"]
        picture = user_info.get("picture")
    except Exception as error:
        logger.exception(f"OAuth2 callback error {error}")
        flash("Prijava nije uspela. Pokusajte ponovo.")
        response = redirect(safe_url_for("login"))
        response.set_cookie('token', 'None', expires=0)
        return response

    user = database.get_user(connection=g.db, email=email)
    if not user:
        return redirect(safe_url_for('complete_registration', email=email))

    token = helper.generate_token()
    database.update_user(connection=g.db, email=email, token=token, picture=picture)

    if user["authorized"] > 0:
        response = make_response(redirect(safe_url_for('index')))
        response.set_cookie('token', token, max_age=appsettings.MAX_COOKIE_AGE,
                            expires=time.time() + appsettings.MAX_COOKIE_AGE)
    else:
        flash("Vas nalog jos uvek nije odobren.")
        response = redirect(safe_url_for("login"))
        response.set_cookie('token', 'None', expires=0)

    return response


@application.route('/', methods=['GET'])
def index():
    user = current_user()
    if not user:
        return redirect(safe_url_for('login'))

    unauthorized_users = database.get_user(connection=g.db, authorized=0)

    resp = make_response(render_template(
        'home.html',
        user=user,
        admin=user["authorized"] > 1,
        unauthorized_users=unauthorized_users,
        title=appsettings.APP_TITLE,
        url_for=safe_url_for
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@application.route('/', methods=['POST'])
def index_post():
    user = current_user()
    if not user:
        return redirect(safe_url_for('login'))

    address = request.form.get('address')
    if address:
        database.update_user(connection=g.db, email=user["email"], address=address)
        flash("Adresa je sacuvana.")

    return redirect(safe_url_for('index'))


@application.route('/complete_registration', methods=['GET', 'POST'])
def complete_registration():
    if request.method == 'GET':
        email = request.args.get('email')
        if not email:
            return "Missing email", 400
        return render_template('complete_registration.html', email=email,
                               title=appsettings.APP_TITLE, url_for=safe_url_for)

    email = request.form.get('email')
    address = request.form.get('address')
    if not email or not address:
        flash("Nedostaju podaci.")
        return redirect(request.url)

    approval_token = helper.generate_token()
    database.add_user(connection=g.db, email=email, token=approval_token, address=address)

    approve_link = f"{request.host_url}approve_user?email={email}&token={approval_token}"
    body = (
        f"Novi korisnik: {email}\n"
        f"Adresa: {address}\n\n"
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

    flash("Registracija je poslata. Dobicete mejl kada je administrator odobri.")
    return redirect(safe_url_for('login'))


@application.route('/approve_user', methods=['GET'])
def approve_user():
    email = request.args.get('email')
    token = request.args.get('token')
    if not email or not token:
        return "Invalid request.", 400

    user = database.get_user(connection=g.db, email=email)
    if not user:
        return "User not found.", 404
    if user.get('token') != token:
        return "Invalid or expired approval token.", 403

    database.update_user(connection=g.db, email=email, authorized=1,
                         token=helper.generate_token())

    try:
        helper.send_email(
            recipient=email,
            subject=f"{appsettings.APP_TITLE}: registracija odobrena",
            body=f"Vasa registracija je odobrena. Mozete se prijaviti na {request.host_url}",
        )
    except Exception as error:
        logger.exception(f"Could not notify {email}: {error}")

    return f"Korisnik {email} je odobren."


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
        database.get_user(connection=g.db, authorized=1)
        + database.get_user(connection=g.db, authorized=2),
        key=lambda u: (u["email"].lower() == user["email"].lower(), u["email"].lower())
    )

    return render_template('manage_users.html', user=user,
                           unauthorized_users=unauthorized_users,
                           authorized_users=authorized_users,
                           title=appsettings.APP_TITLE, url_for=safe_url_for)


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
        database.update_user(connection=g.db, email=email, authorized=1)
    elif action == 'make_admin':
        database.update_user(connection=g.db, email=email, authorized=2)
    elif action == 'remove':
        database.delete_user(connection=g.db, email=email)
    elif action == 'update_address':
        address = request.form.get('address')
        if address:
            database.update_user(connection=g.db, email=email, address=address)

    return redirect(safe_url_for('manage_users'))


@application.route('/run_check', methods=['GET'])
@csrf.exempt
def run_check():
    if request.args.get('token') != appsettings.TRIGGER_TOKEN:
        return "Forbidden", 403

    try:
        # Imported here so the Gemini client is only created when a check actually runs.
        import checker

        result = checker.run_checks()
    except Exception as error:
        logger.exception(f"Check run failed: {error}")
        return f"Check run failed: {error}", 500

    if result["skipped"]:
        return f"Provera za {result['date']} je vec uspesno obavljena danas.\n", 200, \
               {"Content-Type": "text/plain; charset=utf-8"}

    if result["errors"]:
        logger.error(f"Check run errors: {result['errors']}")

    return (
        f"Datum: {result['date']}\n"
        f"Provereno korisnika: {result['checked']}\n"
        f"Obavesteno: {', '.join(result['notified']) or '-'}\n"
        f"Greske: {'; '.join(result['errors']) or '-'}\n"
    ), 200, {"Content-Type": "text/plain; charset=utf-8"}

if __name__ == "__main__":
    application.run(debug=False, host="0.0.0.0", port=5000)
