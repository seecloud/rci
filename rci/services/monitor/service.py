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
from collections import defaultdict
import functools
import dbm
import json
import os
import pkgutil
from concurrent.futures import FIRST_COMPLETED
import logging
from rci.common import github


LOG = logging
ANONYMOUS_METHODS = {"listTasks", "connectJob", "disconnectJob"}
OPERATOR_METHODS = {"getJobsConfigs", "startJob"}
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
        self._ws_job_listeners = defaultdict(list)
        self._jobws_cb_map = defaultdict(list)

    def _broadcast(self, data):
        data = json.dumps(data)
        for ws in self.connections:
            ws.send_str(data)

    def cb_console(self, job, stream, data):
        LOG.debug("%s '%s'", job, data)

    def cb_task_started(self, event):
        LOG.debug("START %s", event)
        self._broadcast(["taskUpdate", event.to_dict()])
        for job in event.jobs:
            job.console_callbacks.append(functools.partial(self.cb_console, job))

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
        self.root.emit(event)
        return json.dumps(["jobStartOk", event.to_dict()])

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

        ws.send_str(json.dumps(["authOk", {"login": login, "admin": admin, "operator": operator}]))
        events = []
        for event in self.root.task_event_map.values():
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
                    ws.send_str(await method(ws, *args))
                else:
                    LOG.info("Unknown websocket method %s", method_name)
            else:
                print(msg.type, msg)
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
            print(request.method)
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


class Event:

    def __init__(self, root, env, data):
        self.root = root
        self.env = env
        self.data = data
        self.id = base64.b32encode(os.urandom(10)).decode("ascii")
        self.jobs = []

        self.tasks = []
        self.task_job_map = {}
        self.name_job_map = {}

        self.jobs = [Job(root, jc, env) for jc in self.get_job_confs()]
        self.name = data["job"]
        self.name_job_map[self.name] = self.jobs[0]
        self._stop_event = asyncio.Event(loop=self.root.loop)
        self.status = "pending"

    def stop(self):
        self._stop_event.set()

    def job_done_cb(self, job, task):
        print("done", job, task)

    def get_job_confs(self):
        return [self.root.config.data["job"][self.data["job"]]]

    async def _all_jobs_finished_cb(self):
        LOG.info("%s job finished cb", self)

    async def _start_job(self, jc):
        provider =  self.root.providers[jc["provider"]]
        cluster = await provider.get_cluster(jc["cluster"])
        job = Job(self.root, jc, self.env)
        self.jobs.append(job)
        task = self.root.loop.create_task(job.run())
        self.tasks.append(task)
        self.task_job_map[task] = job

    async def run(self):
        for job in self.jobs:
            task = self.root.loop.create_task(job.run())
            self.task_job_map[task] = job

        while self.task_job_map:
            done, pending = await asyncio.wait(list(self.task_job_map.keys()),
                                               return_when=FIRST_COMPLETED)
            for task in done:
                job = self.task_job_map.pop(task)
                self.job_done_cb(job, task)
                self.root.start_coro(job.cleanup())
        self.status = "finished"
        LOG.info("%s: all jobs finished.", self)

    def to_dict(self):
        jobs = {}
        for job in self.jobs:
            jobs[job.config["name"]] = job.to_dict()
        return {"id": self.id, "name": self.name, "jobs": jobs,
                "status": self.status}


class Job:
    def __init__(self, root, config, env):
        self.root = root
        self.config = config
        self.env = env
        self.console_callbacks = []
        self.status_callbacks = []
        self.status = "queued"

    async def _get_cluster(self):
        provider = self.root.providers[self.config["provider"]]
        self.cluster = await provider.get_cluster(self.config["cluster"])

    def _console_cb(self, stream, data):
        for cb in self.console_callbacks:
            cb(stream, data)

    def _update_status(self, status):
        self.status = status
        for cb in self.status_callbacks:
            cb(status)

    async def run(self):
        _out_cb = functools.partial(self._console_cb, 1)
        _err_cb = functools.partial(self._console_cb, 2)
        self._update_status("boot")
        await asyncio.shield(self._get_cluster())
        for vm, scripts in self.config["scripts"].items():
            vm = self.cluster.vms[vm]
            for script_name in scripts:
                script = self.root.config.get_script(script_name)
                self.root.log.debug("%s: running script: %s", self, script)
                await vm.ssh.wait()
                self._update_status("running " + script_name)
                error = await vm.run_script(script, self.env, _out_cb, _err_cb)
                if error:
                    self.root.log.debug("%s error in script %s", self, script)
                    return error
        self.root.log.debug("%s all scripts success", self)

    async def cleanup(self):
        await self.cluster.delete()

    def to_dict(self):
        return {
            "name": self.config["name"],
            "status": self.status,
        }

    def __str__(self):
        return "<Job %s>" % self.config["name"]

    __repr__ = __unicode__ = __str__
