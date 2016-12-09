# Copyright 2015: Mirantis Inc.
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

import abc
import asyncio
import base64
from concurrent.futures import FIRST_COMPLETED
import logging
import os

LOG = logging


class VM(abc.ABC):
    ssh = None
    name = None

    @abc.abstractmethod
    async def run_script(self, script):
        """Run script.

        :param dict script: script instance from config
        """
        pass

    @abc.abstractmethod
    async def publish_path(self, src_path, dst_path):
        pass


class SSHVM(VM):

    async def run_script(self, loop, script, env, stdout_cb, stderr_cb):
        cmd = script.get("interpreter", "/bin/bash -xe -s")
        username = username=script.get("username")
        LOG.debug("Getting ssh for user %s (%s)", username, self)
        ssh = self.get_ssh(loop, username=username)
        await ssh.wait()
        e = await ssh.run(cmd, stdin=script["data"], env=env,
                          stdout=stdout_cb, stderr=stderr_cb,
                          check=False)
        if e:
            return e


class Cluster:

    def __init__(self, provider):
        self.provider = provider
        self.networks = {}
        self.vms = {}
        self.env = {}

    async def delete(self):
        return await self.provider.delete_cluster(self)

    def __str__(self):
        return "<Cluster %s>" % self.vms


class Provider(abc.ABC):

    def __init__(self, root, config):
        """
        :param Root root:
        :param dict config: full provider config
        """
        self.root = root
        self.config = config
        self.name = config["name"]
        self.clusters = []

    @abc.abstractmethod
    async def start(self):
        pass

    @abc.abstractmethod
    async def get_cluster(self, name):
        """Boot vms and return Cluster instance.

        :param name: cluster name
        """
        pass


class Event(abc.ABC):

    def __init__(self, root, env, data):
        self.root = root
        self.env = env
        self.data = data
        self.id = base64.b32encode(os.urandom(10)).decode("ascii")

        self.title = self.get_title()
        self.jobs = self.get_jobs()
        self.status = "pending"

        self.tasks = []
        self.task_job_map = {}

        self.name_job_map = {}
        for job in self.jobs:
            self.name_job_map[job.config["name"]] = job

    @abc.abstractmethod
    def get_title(self):
        pass

    @abc.abstractmethod
    def get_jobs(self):
        pass

    def update_status_cb(self, job):
        LOG.debug("Job updated %s", job)
        self.root.notify_services("cb_task_updated", self)

    def job_finished_cb(self, job, task):
        try:
            task.result()
        except Exception as ex:
            job._update_status("error")
            LOG.exception("Error running job")
        LOG.info("Job %s done", job)

    async def run(self):
        for job in self.jobs:
            task = self.root.loop.create_task(job.run())
            self.task_job_map[task] = job

        while self.task_job_map:
            done, pending = await asyncio.wait(list(self.task_job_map.keys()),
                                               return_when=FIRST_COMPLETED)
            for task in done:
                job = self.task_job_map.pop(task)
                self.job_finished_cb(job, task)
                self.root.start_coro(job.cleanup())
        self.status = "finished"
        LOG.info("%s: all jobs finished.", self)

    def to_dict(self):
        jobs = {}
        for job in self.jobs:
            jobs[job.config["name"]] = job.to_dict()
        return {"id": self.id, "name": self.title, "jobs": jobs,
                "status": self.status}
