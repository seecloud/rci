# Copyright 2016: Mirantis Inc.
# All Rights Reserved.
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import asyncio
import base64
from collections import defaultdict
import functools
import json
import logging
import pkgutil
import dbm
import os

from rci import event
from rci import job
from rci.task import Task
from rci.common import github

from aiohttp import web
import yaml

LOG = logging


class Event(event.Event):

    def __init__(self, root, env, data, client, jobs_type):
        pr = data["pull_request"]
        self.project = data["repository"]["full_name"]
        self.head = pr["head"]["sha"]
        self.url = pr["url"]
        self.jobs_type = jobs_type
        self.env = {
            "GITHUB_REPO": self.project,
            "GITHUB_HEAD": self.head,
        }
        if pr["head"]["repo"]["full_name"] != self.project:
            self.env["GITHUB_REMOTE"] = pr["head"]["repo"]["clone_url"]
        super().__init__(root, env, data)

    def get_title(self):
        return self.data["pull_request"]["title"]

    def get_jobs(self):
        jobs = []
        for jc in self.root.config.get_jobs(self.project, self.jobs_type):
            jobs.append(job.Job(self, jc, self.env))
        return jobs

    def job_started_cb(self, job):
        data = {
            "state": "pending",
            "context": job.name,
            "description": "pending...",
            "target_url": self.root.config.core["logs-url"] + job.id,
        }
        self.root.start_coro(self.client.post("/repos/:repo/statuses/:sha",
                                              self.project, self.head, **data))

    def job_finished_cb(self, job, state):
        data = {
            "state": state,
            "description": state,
            "target_url": self.root.config.core["logs-url"] + job.id,
            "context": job.name,
        }
        self.root.start_coro(self.client.post("/repos/:repo/statuses/:sha",
                                              self.project, self.head, **data))


def session_resp(handler):
    @functools.wraps(handler)
    async def wrapper(self, request):
        response = await handler(self, request)
        session = request.get("session")
        if session is not None:
            session.set_cookie(response)
            session.save()
        return response
    return wrapper


class Session:

    def __init__(self, ss, sid):
        self.ss = ss
        self.sid = sid
        data = self.ss.store.get(self.sid)
        self.data = json.loads(data.decode("ascii")) if data else dict()

    def set_cookie(self, response):
        response.set_cookie(self.ss.cookie_name, self.sid)

    def save(self):
        self.ss.store[self.sid] = json.dumps(self.data)


class SessionStore:

    def __init__(self, store, cookie_name):
        self.store = store
        self.cookie_name = cookie_name

    def session(self, request):
        sid = request.cookies.get(self.cookie_name)
        if sid is None:
            sid = base64.b64encode(os.urandom(15)).decode('ascii')
        self.sid = sid
        session = Session(self, sid)
        request["session"] = session
        return session


class Service:

    def __init__(self, root, **kwargs):
        self.root = root
        self.cfg = kwargs
        self.http_path = kwargs.get("http_path", "github")
        self.oauth = github.OAuth(**root.config.secrets[self.cfg["name"]])

        store = kwargs["data-path"]
        os.makedirs(store, exist_ok=True)
        self.users = dbm.open(os.path.join(store, "users.db"), "cs")
        self.orgs = dbm.open(os.path.join(store, "orgs.db"), "cs")
        session_store = dbm.open(os.path.join(store, "sessions.db"), "cs")
        self.ss = SessionStore(session_store, kwargs.get("cookie_name", "ghs"))

    async def http_handler(self, request):
        path = request.path.split("/")[2]
        LOG.info("%s %s", request, path)
        handler = getattr(self, "_http_%s" % path, None)
        if handler:
            return await handler(request)
        return web.HTTPNotFound()

    async def _webhook_push(self, request, data):
        print(data)

    async def _webhook_pull_request(self, request, data):
        if data["action"] in ("opened", "synchronize"):
            self.root.log.info("Emiting event")
            owner = data["repository"]["owner"]
            owner_type = str(owner["type"]).encode("ascii")
            owner_id = str(owner["id"]).encode("ascii")
            if owner_type == b"Organization":
                token = self.orgs[owner_id].decode("ascii")
            else:
                token = self.users[owner_id].decode("ascii")
            client = github.Client(token)
            self.root.emit(Event(self.root, {}, data, client, "cr"))
        else:
            self.root.log.debug("Skipping event %s" % data["action"])

    async def _http_webhook(self, request):
        event = request.headers["X-Github-Event"]
        handler = getattr(self, "_webhook_%s" % event, None)
        if handler is None:
            self.root.log.debug("Unknown event")
            self.root.log.debug(str(request.headers))
            return web.Response(text="ok")
        self.root.log.info("Event: %s" % event)
        await handler(request, await request.json())
        return web.Response(text="ok")

    def _get_client(self, request):
        session = self.ss.session(request)
        token = session.data.get("token")
        if token:
            return github.Client(token)

    @session_resp
    async def _http_oauth2(self, request):
        client = await self.oauth.oauth(request.GET["code"], request.GET["state"])
        user_data = await client.get("user")
        session = self.ss.session(request)
        self.users[str(user_data["id"])] = client.token
        session.data["token"] = client.token
        session.data["user"] = user_data["login"]
        response = web.HTTPFound(self.url + "/settings")
        return response

    async def _http_settings(self, request):
        import jinja2
        template = jinja2.Template(
                pkgutil.get_data("rci.services.github", "github_settings.html").decode("utf8"))
        client = self._get_client(request)
        if client is None:
            return web.HTTPUnauthorized(text="fail")
        orgs = []
        for org in (await client.get("user/orgs")):
            orgs.append(org)
        return web.Response(text=template.render(orgs=orgs), content_type="text/html")

    async def _http_add_org_webhook(self, request):
        await request.post()
        data = {
            "name": "web",
            "active": True,
            "events": ["*"],
            "config": {
                "url": self.root.config.core["url"] + self.url + "webhook",
                "content_type": "json"
            },
        }
        client = self._get_client(request)
        resp = await client.post("/orgs/:org/hooks", request.POST["org"], **data)
        org_data = await client.get("/orgs/:org", request.POST["org"])
        self.orgs[str(org_data["id"]).encode("ascii")] = client.token
        return web.Response(text=str(resp))

    async def _http_login(self, request):
        url = self.oauth.generate_request_url(("read:org", ))
        return web.HTTPFound(url)

    async def _http_registraion(self, request):
        url = self.oauth.generate_request_url(
            ("repo:status", "write:repo_hook", "admin:org_hook", "read:org"))
        return web.HTTPFound(url)

    async def handle_pr(self, request):
        await asyncio.sleep(1)

    async def run(self):
        await asyncio.Event(loop=self.root.loop).wait()

    @staticmethod
    async def check_config(config, service_name):
        """
        :param rci.config.Config config:
        :param str service_name:
        """
        pass
