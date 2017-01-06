# Copyright 2017: Mirantis Inc.
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

from rci import base
from rci.common.ssh import SSH

class Provider(base.Provider):

    async def start(self):
        pass

    async def get_cluster(self, name):
        cluster = base.Cluster(self)
        for name, conf in self.config["clusters"][name].items():
            cluster.vms[name] = VM(conf)
        return cluster

    async def delete_cluster(self, cluster):
        pass


class VM(base.SSHVM):

    def __init__(self, ssh_kwargs):
        self.ssh_kwargs = ssh_kwargs

    def get_ssh(self, loop, username=None):
        if username:
            kwargs = self.ssh_kwargs.copy()
            kwargs.update({"username": username})
        else:
            kwargs = self.ssh_kwargs
        return SSH(loop, **kwargs)
