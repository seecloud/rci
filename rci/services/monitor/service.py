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
import dbm
import json
import os
import pkgutil
from concurrent.futures import FIRST_COMPLETED
import logging
from rci.common import github


LOG = logging


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

    async def _list_jobs(self):
        pass

    async def _ws_getJobInfo(self, job_id):
        LOG.debug("getJobInfo %s", job_id)
        return json.dumps(["jobInfo", {
            "name": "test-something<b>ok</b>",
            "status": "pending",
        }])

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
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                method_name, args = json.loads(msg.data)
                method = getattr(self, "_ws_" + method_name, None)
                if method:
                    ws.send_str(await method(*args))
            else:
                print(msg.type, msg)
        return ws

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

    def __init__(self, root, data):
        self.root = root
        self.data = data
        self.tasks = []
        self.task_job_map = {}

    def job_done_cb(self, job, task):
        print("done", job, task)

    def get_job_confs(self):
        return [self.root.conf.data["job"][self.data["job"]]]

    async def _start_job(self, jc):
        provider =  self.root.providers[jc["provider"]]
        cluster = await provider.get_cluster(jc["cluster"])
        job = Job(jc, self.env, cluster)
        task = self.tasks.append(self.root.loop.create_task(job.run()))
        self.task_job_map[task] = job

    async def run(self):
        try:
            for jc in self.get_job_confs():
                await asyncio.shield(self._start_job(js))
        except asyncio.CancelledError:
            for task in self.task_job_map:
                task.cancel()
        cleanups = []
        while self.task_job_map:
            done, pending = await asyncio.wait(list(self.task_job_map.keys()),
                                               return_when=FIRST_COMPLETED)
            for task in done:
                job = self.task_job_map.pop(task)
                self.job_done_cb(job, task)
                cleanups.append(self.root.loop.create_task(job.cluster.delete()))
        await asyncio.wait(cleanups)


class Job:
    def __init__(self, config, env, cluster):
        self.config = config
        self.cluster = cluster

    async def run(self):
        await asyncio.sleep(1)
