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
import logging

from rci import base
from rci.common import openstack
from rci.common.ssh import SSH

LOG = logging


class Provider(base.Provider):

    def __init__(self, root, *args, **kwargs):
        super().__init__(root, *args, **kwargs)
        self._ready = asyncio.Event(loop=root.loop)
        self._get_cluster_lock = asyncio.Lock()
        self._vms_semaphore = asyncio.Semaphore(self.config["max_vms"])

    async def stop(self):
        await asyncio.sleep(0)

    async def start(self):
        self.access_net = self.config["ssh"]["access_net"]
        self.ssh_keys = [self.config["ssh"]["private_key_path"]]
        self.jumphost = self.config["ssh"].get("jumphost")
        if self.jumphost:
            self.jumphost = SSH(self.root.loop, keys=self.ssh_keys, **self.jumphost)
            await self.jumphost.wait()
        secrets = self.root.config.secrets[self.name]
        self.client = openstack.Client(secrets["auth_url"],
                                       secrets["username"],
                                       secrets["tenant"],
                                       cafile=secrets["cafile"])
        await self.client.login(password=secrets["password"])
        self.network_ids = {}
        self.image_ids = {}
        self.flavor_ids = {}
        for network in (await self.client.list_networks())["networks"]:
            self.network_ids[network["name"]] = network["id"]

        for image in (await self.client.list_images())["images"]:
            self.image_ids[image["name"]] = image["id"]

        for item in (await self.client.list_flavors())["flavors"]:
            self.flavor_ids[item["name"]] = item["id"]

        self._ready.set()

    async def delete_cluster(self, cluster):
        for vm in cluster.vms.values():
            await self.client.delete_server(vm.uuid, wait=True)
            self._vms_semaphore.release()
        for uuid in cluster.networks.values():
            await self.client.delete_network(uuid)

    async def _create_server(self, server_name, image_id, flavor_id,
                             networks, ssh_key_name, user_data):
        await self._vms_semaphore.acquire()
        server = await self.client.create_server(
            server_name, image_id, flavor_id, networks,
            ssh_key_name, user_data)
        return server

    async def get_cluster(self, name):
        async with self._get_cluster_lock:
            return await self._get_cluster(name)

    async def _get_cluster(self, name):
        await self._ready.wait()
        default_user = self.config["ssh"]["default_username"]
        cluster = base.Cluster(self)
        servers = []
        for vm_name, vm_conf in self.config["clusters"][name].items():
            networks = []
            for if_type, if_name in vm_conf["interfaces"]:
                if if_type == "dynamic":
                    uuid = cluster.networks.get(if_name)
                    if uuid is None:
                        network = await self.client.create_network(if_name)
                        uuid = network["network"]["id"]
                        subnet = await self.client.create_subnet(uuid)
                        cluster.networks[if_name] = uuid
                else:
                    uuid = self.network_ids[if_name]
                networks.append({"uuid": uuid})
            server = await self._create_server(
                vm_name, self.image_ids[vm_conf["image"]],
                self.flavor_ids[vm_conf["flavor"]],
                networks, self.config["ssh"]["key_name"],
                vm_conf.get("user_data", ""))
            servers.append(server)
        for server in servers:
            data = await self.client.wait_server(server["server"]["id"], delay=4,
                                                 status="ACTIVE",
                                                 error_statuses=["ERROR"])
            ports = await self.client.list_ports(device_id=data["server"]["id"])
            kwargs = {"allowed_address_pairs": [{"ip_address": "0.0.0.0/0"}]}
            for port in ports["ports"]:
                resp = await self.client.update_port(port["id"], **kwargs)
                LOG.debug("Updated port %s, %s", port, resp)
            addresses = data["server"]["addresses"]
            ip = addresses.get(self.access_net)
            if ip is not None:
                ip = ip[0]["addr"]
            else:
                ip = list(addresses.values())[0][0]["addr"]
            vm_name = data["server"]["name"]
            vm_conf = self.config["clusters"][name][vm_name]
            LOG.debug("Creating VM %s", vm_conf)
            vm = VM(data["server"]["id"], vm_name, ip=ip,
                    username=vm_conf.get("username", default_user),
                    keys=self.ssh_keys,
                    password=vm_conf.get("password"),
                    jumphost=self.jumphost)
            LOG.debug("Created VM %s", vm)
            cluster.vms[vm_name] = vm
            cluster.env["RCI_SERVER_" + vm_name] = ip
        LOG.debug("Created cluster %s", cluster)
        return cluster


class VM(base.SSHVM):

    def __init__(self, uuid, name, ip=None, username=None,
                 keys=None, password=None, jumphost=None):
        self.ip = ip
        self.uuid = uuid
        self.name = name
        self.keys = keys
        self.username = username
        self.password = password
        self.jumphost = jumphost

    def get_ssh(self, loop, username=None):
        LOG.debug("Creating ssh for %s@%s", username, self.ip)
        return SSH(loop,
                   hostname=self.ip,
                   username=username or self.username,
                   keys=self.keys,
                   password=self.password,
                   jumphost=self.jumphost)

    def __str__(self):
        return "<OpenStack VM %s (%s@%s)>" % (self.uuid, self.username, self.ip)

    async def publish_path(self, src, dst):
        pass

    __unicode__ = __repr__ = __str__
