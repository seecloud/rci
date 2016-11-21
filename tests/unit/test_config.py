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
            "scripts": {
                "test-cluster": ["env"],
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


class ConfigTestCase(unittest.TestCase):

    def test_get_jobs(self):
        c = _get_default_config_instance()
        expected = {
            'scripts': {'test-cluster': ['env']},
            'name': 'pytest',
            'provider': 'os'
        }
        self.assertEqual(expected, c.get_jobs("project-1", "push"))
    
    def test_get_script(self):
        c = _get_default_config_instance()
        s = c.get_script("env")
        expected = {'data': 'env', 'interpreter': '/bin/sh',
                    'name': 'env', 'user': 'rci'}
        self.assertEqual(expected, s)

    def test_validation_unknown_job(self):
        cfg = _get_configuration()
        cfg.append(
            {"matrix": {
                "name": "unknownjob",
                "projects": "project-1",
                "change-request": ["nonexistent-job"]
            }})
        c = config.Config(mock.Mock())
        cf = _get_conf_tmpfile(cfg)
        self.assertRaises(config.ConfigError, c.load, cf.name)
