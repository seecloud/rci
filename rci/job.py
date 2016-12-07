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
import functools


class Job:
    def __init__(self, event, config, env):
        self.event = event
        self.root = event.root
        self.config = config
        self.env = env
        self.console_callbacks = []
        self.status_callbacks = []
        self.status = "queued"

    async def _get_cluster(self):
        provider = self.root.providers[self.config["provider"]]
        self.cluster = await provider.get_cluster(self.config["cluster"])
        self.env.update(self.cluster.env)

    def _console_cb(self, stream, data):
        for cb in self.console_callbacks:
            cb(stream, data)

    def _update_status(self, status):
        self.status = status
        self.event.update_status_cb(self)
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
                self._update_status("running " + script_name)
                error = await vm.run_script(self.root.loop, script, self.env,
                                            _out_cb, _err_cb)
                if error:
                    self.root.log.debug("%s error in script %s", self, script)
                    self._update_status("failure")
                    return error
        self._update_status("success")
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
