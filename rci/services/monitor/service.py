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

import aiohttp
from aiohttp import web
import asyncio
import base64
import collections
import functools
import dbm
import json
import os
import pkgutil
import logging
from rci import event
from rci import job
from rci.common import github

LOG = logging
ANONYMOUS_METHODS = {"listTasks", "connectJob", "disconnectJob", "ping"}
OPERATOR_METHODS = {"getJobsConfigs", "startJob", "endJob"}
ADMIN_METHODS = {}


class Service:

    def __init__(self, root, **config):
        self.root = root
        self.config = config

        store = self.config["dbm_path"]
        os.makedirs(store, exist_ok=True)

        self.http_path = config.get("http_path", "monitor")
        self.oauth = github.OAuth(**root.config.secrets[self.config["name"]])
        self.tokens = dbm.open(os.path.join(store, "tokens.db"), "cs")
        self.sessions = dbm.open(os.path.join(store, "sessions.db"), "cs")
        self.acl = config["access"]
        self.logs_path = config["logs_path"]
        os.makedirs(self.logs_path, exist_ok=True)
        self.connections = []
        self._ws_job_listeners = collections.defaultdict(list)
        self._jobws_cb_map = collections.defaultdict(list)
        self._ws_user_map = {}
        self._user_events = collections.defaultdict(list)

    def _broadcast(self, data):
        data = json.dumps(data)
        for ws in self.connections:
            ws.send_str(data)

    def cb_job_started(self, job):
        path = os.path.join(self.config["jobs-logs"], job.event.id,
                            job.config["name"])
        os.makedirs(path)
        path = os.path.join(path, "console.html")
        outfile = open(path, "wb")
        job.console_callbacks.append(functools.partial())

    def cb_task_updated(self, event):
        LOG.debug("UPDATE %s", event)
        self._broadcast(["taskUpdate", event.to_dict()])

    def cb_task_started(self, event):
        LOG.debug("START %s", event)
        self._broadcast(["taskUpdate", event.to_dict()])

    def cb_task_finished(self, event):
        LOG.debug("END %s", event)
        self._broadcast(["taskUpdate", event.to_dict()])

    async def _ws_connectJob(self, ws, event_id, job_name):
        event = self.root.eventid_event_map.get(event_id, None)
        if event:
            def cb(stream, data):
                try:
                    ws.send_str(json.dumps(["consoleData", [stream, data]]))
                except Exception:
                    LOG.exception("error sending console data")
            job = event.name_job_map[job_name]
            job.console_callbacks.append(cb)
            self._ws_job_listeners[ws].append(job)
            self._jobws_cb_map[(job, ws)].append(cb)
            return json.dumps(["jobConnected", [event.to_dict(), job.config["name"]]])
        else:
            return "404"

    async def _ws_disconnectJob(self, ws, event_json, job_name):
        LOG.debug("WS disconnect %s %s", event_json, job_name)
        self._disconnect_ws_console(ws)
        return json.dumps(["jobDisconnected", []])

    async def _ws_ping(self, ws):
        return """["pong", []]"""

    async def _ws_getTaskInfo(self, ws, event_id):
        event = self.root.eventid_event_map.get(job_id, None)
        if event:
            return json.dumps(["taskInfo", {
                "id": event.id,
                "jobs": [j.to_dict() for j in event.jobs],
            }])

    async def _ws_getJobsConfigs(self, ws):
        data = {
            "scripts": self.root.config.data["script"],
            "jobs": list(self.root.config.data["job"].values()),
        }
        return json.dumps(["jobsConfigs", data])

    async def _ws_startJob(self, ws, job_name):
        LOG.info("Starting custom job %s", job_name)
        env = {"GITHUB_REPO": "seecloud/automation", "GITHUB_HEAD": "master"}
        data = {"job": job_name}
        event = Event(self.root, env, data)
        self._user_events[self._ws_user_map[ws]["login"]].append(event)
        ws.send_str(json.dumps(["jobStartOk", event.to_dict()]))
        self.root.emit(event)

    async def _ws_endJob(self, ws, event_id):
        events = self._user_events[self._ws_user_map[ws]["login"]]
        for event in events:
            if event.id == event_id:
                event.stop_event.set()

    async def _check_acl(self, method_name, client, operator, admin):
        if method_name in ANONYMOUS_METHODS:
            return True
        if operator and (method_name in OPERATOR_METHODS):
            return True
        if admin and (method_name in ADMIN_METHODS):
            return True

    async def ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        sid = request.cookies.get(self.config["cookie_name"])
        client = None
        admin = False
        operator = False
        login = None
        if sid:
            login = self.sessions.get(sid)
            if login:
                token = self.tokens.get(login).decode("ascii")
                if token:
                    client = github.Client(token)
        if client:
            login = login.decode("utf8")
            if login in self.acl["admin"]["users"]:
                admin = True
            if login in self.acl["operator"]["users"]:
                operator = True

            orgs = await client.get("user/orgs")
            for org in orgs:
                if org["login"] in self.acl["admin"]["orgs"]:
                    admin = True
                if org["login"] in self.acl["operator"]["orgs"]:
                    operator = True
        user = {"login": login, "admin": admin, "operator": operator}
        self._ws_user_map[ws] = user
        ws.send_str(json.dumps(["authOk", user]))

        user_events = self._user_events[login]
        if user_events:
            ws.send_str(json.dumps(["userTasks", [e.id for e in user_events]]))

        for event in self.root.get_tasks():
            ws.send_str(json.dumps(["taskUpdate", event.to_dict()]))

        self.connections.append(ws)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                method_name, args = json.loads(msg.data)
                if not (await self._check_acl(method_name, client, operator, admin)):
                    ws.send_str(json.dumps(["accessDenied", method_name]))
                    continue
                method = getattr(self, "_ws_" + method_name, None)
                if method:
                    ret = await method(ws, *args)
                    if ret is not None:
                        ws.send_str(ret)
                else:
                    LOG.info("Unknown websocket method %s", method_name)
            else:
                LOG.debug("websocket msg %s %s", msg.type, msg)
        self._ws_user_map.pop(ws)
        self.connections.remove(ws)
        self._disconnect_ws_console(ws)
        return ws

    def _disconnect_ws_console(self, ws):
        for job in self._ws_job_listeners.get(ws, []):
            for cb in self._jobws_cb_map.pop((job, ws), []):
                job.console_callbacks.remove(cb)

    async def _oauth2_handler(self, request):
        client = await self.oauth.oauth(request.GET["code"], request.GET["state"])
        user_data = await client.get("user")
        login = user_data["login"].encode("utf8")
        self.tokens[login] = client.token
        sid = base64.b32encode(os.urandom(15)).decode('ascii')
        self.sessions[sid] = login
        resp = web.HTTPFound("/monitor/")
        resp.set_cookie(self.config["cookie_name"], sid)
        return resp

    async def http_handler(self, request):
        if request.path.endswith("api.sock"):
            return await self.ws_handler(request)
        if request.path.endswith("/monitor/"):
            data = pkgutil.get_data("rci.services.monitor",
                                    "monitor.html").decode("utf8")
            return web.Response(text=data, content_type="text/html")
        if request.path.endswith("/login/github"):
            if request.method == "POST":
                url = self.oauth.generate_request_url(("read:org", ))
                return web.HTTPFound(url)
        if request.path.endswith("/oauth2/github"):
            return (await self._oauth2_handler(request))
        if request.path.endswith("logout"):
            if request.method == "POST":
                sid = request.cookies.get(self.config["cookie_name"])
                del(self.sessions[sid])
                return web.HTTPFound("/monitor/")
        return web.HTTPNotFound()

    async def run(self):
        await asyncio.Event().wait()

    def __str__(self):
        return "<Monitor %s>" % self.http_path

    __unicode__ = __repr__ = __str__


class Event(event.Event):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_event = asyncio.Event(loop=self.root.loop)

    def stop(self):
        self.stop_event.set()

    def get_title(self):
        return self.data["job"]

    def get_jobs(self):
        jc = self.root.config.data["job"][self.data["job"]]
        return [Job(self, jc, self.env)]


class Job(job.Job):

    async def run(self):
        await super().run()
        await self.event.stop_event.wait()
