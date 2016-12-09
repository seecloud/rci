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
import os
import logging


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
        logging.debug("Job updated %s", job)
        self.root.notify_services("cb_task_updated", self)

    def job_started_cb(self, job, task):
        self.root.notify_services("cb_job_started", job)

    def job_finished_cb(self, job, task):
        try:
            task.result()
        except Exception as ex:
            job._update_status("error")
            logging.exception("Error running job")
        logging.info("Job %s done", job)
        self.root.notify_services("cb_job_finished", job)

    async def run(self):
        for job in self.jobs:
            task = self.root.loop.create_task(job.run())
            self.task_job_map[task] = job
            self.job_started_cb(job, task)

        while self.task_job_map:
            done, pending = await asyncio.wait(list(self.task_job_map.keys()),
                                               return_when=FIRST_COMPLETED)
            for task in done:
                job = self.task_job_map.pop(task)
                self.job_finished_cb(job, task)
                self.root.start_coro(job.cleanup())
        self.status = "finished"
        logging.info("%s: all jobs finished.", self)

    def to_dict(self):
        jobs = {}
        for job in self.jobs:
            jobs[job.config["name"]] = job.to_dict()
        return {"id": self.id, "name": self.title, "jobs": jobs,
                "status": self.status}
