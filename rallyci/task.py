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

import asyncio
from concurrent import futures
import cgi

from rallyci.job import Job
from rallyci import utils

def human_time(seconds):
    seconds = int(seconds)
    result = []
    for i in range(len(NAMES) - 1, -1, -1):
        a = seconds // INTERVALS[i]
        if a > 0:
            result.append((a, NAMES[i][1 % a]))
            seconds -= a * INTERVALS[i]
    return ' '.join(''.join(str(x) for x in r) for r in result)


def get_key(event):
    return event["change"]["project"] + event["patchSet"]["ref"]


class Task:
    def __init__(self, stream, project, event):
        self.id = utils.get_rnd_name("EVNT", length=10)
        self.stream = stream
        self.root = stream.root
        self.log = stream.root.log
        self.event = event
        self.project = project
        self.jobs = {}
        self.jobs_list = []
        self.cfg = self.root.config.data["project"][self.project]
        env = self._get_env()
        if event["type"] == "ref-updated":
            for job_name in self.cfg.get("on-ref-updated", []):
                job = Job(self, job_name)
                job.env.update(env)
                self.jobs_list.append(job)
            return
        for job_name in self.cfg.get("jobs", []):
            job = Job(self, job_name)
            job.env.update(env)
            self.jobs_list.append(job)
        for job_name in self.cfg.get("non-voting-jobs", []):
            job = Job(self, job_name)
            job.env.update(env)
            job.voting = False
            self.jobs_list.append(job)

    def __repr__(self):
        return "<Task %s %s>" % (self.project, self.id)

    def _get_env(self):
        env = self.stream.cfg.get("env")
        r = {}
        if not env:
            return {}
        for k, v in env.items():
            value = dict(self.event)
            try:
                for key in v.split("."):
                    value = value[key]
                r[k] = value
            except KeyError:
                pass
        return r

    @asyncio.coroutine
    def run_jobs(self):
        try:
            yield from self._run_jobs()
        except asyncio.CancelledError:
            self.log.info("Jobs cancelled in task %s" % self)
        except Exception:
            self.log.exception("Failed to run jobs")

    @asyncio.coroutine
    def _run_jobs(self):
        self.log.debug("Starting jobs for event %s" % self)
        fs = {}
        for job in self.jobs_list:
            fs[(asyncio.async(job.run(), loop=self.root.loop))] = job
        try:
            yield from self.root.wait_fs(fs)
        except asyncio.CancelledError:
            self.log.debug("Cancelled %s" % self)

        if fs:
            self.log.info("Cancelling remainig jobs %s" % list(fs.values()))
            for fut in fs.keys():
                fut.cancel()

        self.log.debug("Waiting jobs cleanup")
        try:
            yield from asyncio.shield(self.root.wait_fs(fs))
        except:
            self.log.exception("WAIT FAIL")
        self.log.debug("Waiting jobs cleanup done")
        if fs:
            self.log.error("Some jobs still pending: %s" % fs)

        key = get_key(self.event) # TODO: move it to gerrit
        self.stream.tasks.remove(key)
        if not self.stream.cfg.get("silent"):
            try:
                yield from self.publish_results()
            except:
                self.log.exception("Failed to publish results for task %s" % self)

    @asyncio.coroutine
    def publish_results(self):
        self.log.debug("Publishing results for task %s" % self)
        comment_header = self.stream.cfg.get("comment-header")
        if not comment_header:
            self.log.warning("No comment-header configured. Can't publish.")
            return
        cmd = ["gerrit", "review"]
        fail = any([j.error for j in self.jobs_list if j.voting])
        if self.stream.cfg.get("vote"):
            cmd.append("--verified=-1" if fail else "--verified=+1")
        succeeded = "failed" if fail else "succeeded"
        summary = comment_header.format(succeeded=succeeded)
        tpl = self.stream.cfg["comment-job-template"]
        for job in self.jobs_list:
            success = job.status + ("" if job.voting else " (non-voting)")
            time = human_time(job.finished_at - job.started_at)
            summary += tpl.format(success=success,
                                  name=job.config["name"],
                                  time=time,
                                  log_path=job.log_path)
            summary += "\n"
        cmd += ["-m", "'%s'" % summary, self.event["patchSet"]["revision"]]
        yield from asyncssh.AsyncSSH(**self.stream.cfg["ssh"]).run(cmd)

    def to_dict(self):
        data = {"id": self.id, "jobs": [j.to_dict() for j in self.jobs_list]}
        subject = self.event.get("change", {}).get("subject", "#####")
        data["subject"] = cgi.escape(subject)
        data["project"] = cgi.escape(self.project)
        # TODO: remove hardcode
        if "patchSet" in self.event:
            uri = self.event["patchSet"]["ref"].split("/", 3)[-1]
            data["url"] = "https://review.openstack.org/#/c/%s" % uri
        else:
            data["url"] = "https://github.com/%s/commit/%s" % (
                    self.project, self.event["refUpdate"]["newRev"])
        return data