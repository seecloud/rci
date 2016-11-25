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
import pkgutil
from concurrent.futures import FIRST_COMPLETED


class Service:

    def __init__(self, root, **config):
        self.root = root
        self.config = config

        self.http_path = config.get("http_path", "monitor")

    async def _list_jobs(self):
        return 

    async def http_handler(self, request):
        data = pkgutil.get_data("rci.services.monitor",
                                "monitor.html").decode("utf8")
        return web.Response(text=data, content_type="text/html")

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
