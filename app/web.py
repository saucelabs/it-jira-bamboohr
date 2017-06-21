import os
import re

import requests
from atlassian_jwt import encode_token
from flask import Flask, render_template, request, redirect
from flask_atlassian_connect import AtlassianConnect
from PyBambooHR import PyBambooHR
from raven.contrib.flask import Sentry
from werkzeug.contrib.fixers import ProxyFix
from json import dumps

from flask_sslify import SSLify

from .client import Client
from .shared import db

try:
    # python2
    from urllib import urlencode
except ImportError:
    # python3
    from urllib.parse import urlencode

app = Flask(
    __name__.split('.').pop(),
    template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
app.wsgi_app = ProxyFix(app.wsgi_app)
sslify = SSLify(app)
app.config.from_object('app.config.%sConfig' %
                       os.environ.get('FLASK_ENV', 'development').title())
db.init_app(app)
if os.environ.get('SENTRY_DSN'):
    sentry = Sentry(app, dsn=os.environ['SENTRY_DSN'])
ac = AtlassianConnect(app, client_class=Client)


@app.template_filter('stripalpha')
def strip_alpha(s):
    return re.sub(r'[^\d+]+', '', s or '')


def get_bamboohr(client):
    return PyBambooHR(
        subdomain=client.bamboohrSubdomain,
        api_key=client.bamboohrApi
    )


@ac.lifecycle('installed')
def installed(client):
    return '', 204


@ac.webpanel(key="userPanel",
             name="Bamboo Employee Information",
             location="atl.jira.view.issue.right.context",
             conditions=[{
                 "condition": "entity_property_equal_to",
                 "params": {
                     "entity": "project",
                     "propertyKey": app.config.get('ADDON_KEY'),
                     "objectName": "isEnabled",
                     "value": "true"
                    }
             }])
def right_context(client):
    issue = request_jira(
        client,
        method='GET',
        url='/rest/api/latest/issue/' + request.args.get('issueKey')
    ).json()
    email = issue['fields']['reporter']['emailAddress']

    bamboo = get_bamboohr(client)
    employee = next((e for e in bamboo.get_employee_directory()
                     if e['workEmail'] == email), None)
    if not employee:
        return '', 404

    employee.update(bamboo.get_employee(int(employee['id'])))

    return render_template(
        'bamboo_user.html',
        xdm_e=request.args.get('xdm_e'),
        employee=employee
    )


@ac.module("configurePage", name="Configure")
def configure_page(client):
    projects = request_jira(
        client,
        method='GET',
        url='/rest/api/2/project?expand=id,key,name,project.properties'
    ).json()
    if request.method.lower() == 'post':
        for project in projects:
            try:
                if request.form.get('project_' + project['id']):
                    request_jira(
                        client,
                        method='PUT',
                        url='/rest/api/2/project/{}/properties/{}'.format(
                            project['id'],
                            app.config.get('ADDON_KEY')),
                        data=dumps({"isEnabled": True})
                    )
                else:
                    request_jira(
                        client,
                        method='DELETE',
                        url='/rest/api/2/project/{}/properties/{}'.format(
                            project['id'],
                            app.config.get('ADDON_KEY'))
                    )
            except requests.HTTPError:
                pass

        try:
            client.bamboohrApi = request.form['bamboohr_api']
            client.bamboohrSubdomain = request.form['bamboohr_subdomain']
            Client.save(client)
            return render_template('configure_page_success.html')
        except ValueError:
            pass

    for project in projects:
        try:
            properties = request_jira(
                client,
                method='GET',
                url='/rest/api/2/project/{}/properties/{}'.format(
                    project['id'],
                    app.config.get('ADDON_KEY'))
            )
            project.update(**properties.json()['value'])
        except requests.HTTPError:
            pass

    return render_template(
        'configure_page.html',
        xdm_e=request.args['xdm_e'],
        bamboohrApi=client.bamboohrApi or '',
        bamboohrSubdomain=client.bamboohrSubdomain or '',
        projects=projects,
    )


def request_jira(client, url, method='GET', **kwargs):
    jwt_authorization = 'JWT %s' % encode_token(
        method, url, app.config.get('ADDON_KEY'),
        client.sharedSecret)
    result = requests.request(
        method,
        client.baseUrl.rstrip('/') + url,
        headers={
            "Authorization": jwt_authorization,
            "Content-Type": "application/json"
        },
        **kwargs)
    try:
        result.raise_for_status()
    except requests.HTTPError as e:
        raise requests.HTTPError(e.response.text, response=e.response)
    return result


@app.route('/healthcheck')
def healthcheck():
    return 'OK', 200


# Move the old path to the new path
@app.route('/addon/descriptor')
def move_descriptor():
    return redirect('/atlassian_connect/descriptor')


@app.route('/')
def index():
    return 'Welcome to Jira Bamboohr Integration'


if __name__ == '__main__':
    app.jinja_env.auto_reload = True
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(
        debug=True,
        port=int(os.environ.get('PORT', "3000")),
        host="0.0.0.0"
    )