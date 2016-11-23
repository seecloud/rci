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

from rci import config
import yaml

import os
import tempfile
import unittest
from unittest import mock


def _get_configuration():
    return [
        {"core": {
            "listen": ["localhost", 8081],
            "url": "https://example.net"
        }},
        {"provider": {
            "name": "os",
            "module": "rci.providers.openstack",
            "clusters": {
                "os-cluster-1": {
                    "os-vm-1": {
                        "image": "image-1",
                    },
                },
            },
        }},
        {"service": {
            "name": "status",
            "module": "rci.services.status",
        }},
        {"script": {
            "name": "env",
            "user": "rci",
            "interpreter": "/bin/sh",
            "data": "env"
        }},
        {"job": {
            "name": "pytest",
            "provider": "os",
            "cluster": "os-cluster-1",
            "scripts": {
                "os-vm-1": ["env"],
            },
        }},
        {"matrix": {
            "name": "matrix-1",
            "projects": ["project-1", "project-2"],
            "change-request": ["pytest"],
            "push": ["pytest"],
        }},
    ]


def _get_conf_tmpfile(configuration=None):
    if configuration is None:
        configuration = _get_configuration()
    config = tempfile.NamedTemporaryFile()
    config.write(yaml.dump(configuration,
                           default_flow_style=False).encode("utf8"))
    config.flush()
    return config


def _get_default_config_instance():
    cf = _get_conf_tmpfile()
    c = config.Config(mock.Mock())
    c.load(cf.name)
    return c


def _append_and_get_config(append):
    cfg = _get_configuration()
    cfg.append(append)
    c = config.Config(mock.Mock())
    cf = _get_conf_tmpfile(cfg)
    return c.load(cf.name)


class ConfigTestCase(unittest.TestCase):

    def test_get_jobs(self):
        c = _get_default_config_instance()
        expected = [{
            'scripts': {'os-vm-1': ['env']},
            'name': 'pytest',
            'provider': 'os',
            "cluster": "os-cluster-1"
        }]
        self.assertEqual(expected, c.get_jobs("project-1", "push"))
    
    def test_get_script(self):
        c = _get_default_config_instance()
        s = c.get_script("env")
        expected = {'data': 'env', 'interpreter': '/bin/sh',
                    'name': 'env', 'user': 'rci'}
        self.assertEqual(expected, s)

    def test_validation_unknown_job(self):
        self.assertRaisesRegexp(config.ConfigError, "nonexistent-job",
            _append_and_get_config,
            {"matrix": {
                "name": "unknownjob",
                "projects": "project-1",
                "cr": ["nonexistent-job"]
            }})

    def test_validation_unknown_script(self):
        self.assertRaisesRegexp(config.ConfigError, "nonexistent-script",
            _append_and_get_config,
            {"job": {
                "name": "unknownscript",
                "provider": "os",
                "cluster": "os-cluster-1",
                "scripts": {"os-vm-1": ["nonexistent-script"]},
            }})

    def test_validation_unknown_provider(self):
        self.assertRaisesRegexp(config.ConfigError, "nonexistent-provider",
            _append_and_get_config,
            {"job": {
                "name": "unknownprovider",
                "provider": "nonexistent-provider",
                "cluster": "os-cluster-2",
                "scripts": {"os-vm-1": ["env"]},
            }})

    def test_validation_unknown_cluster(self):
        self.assertRaisesRegexp(config.ConfigError, "nonexistent-cluster",
            _append_and_get_config,
            {"job": {
                "name": "unknowncluster",
                "provider": "os",
                "cluster": "nonexistent-cluster",
                "scripts": {"os-vm-1": ["env"]},
            }})

    def test_validation_unknown_vm(self):
        self.assertRaisesRegexp(config.ConfigError, "nonexistent-vm",
            _append_and_get_config,
            {"job": {
                "name": "unknownvm",
                "provider": "os",
                "cluster": "os-cluster-1",
                "scripts": {"nonexistent-vm": ["env"]},
            }})
