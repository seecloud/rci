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
from collections import defaultdict
import importlib
import logging
import logging.config
import yaml

from rci import utils


class ConfigError(Exception):
    pass


class Config:
    """Represent rci configuration.

    c = Config(root)
    c.load("/path/to/file.yaml")
    listen = c.data["core"]["listen"]
    some_secret = c.secrets.get("some-secret")
    my_job = c.data["job"]["my-job"]
    my_script = c.data["script"]["my-script"]
    """

    def __init__(self, root):
        self.root = root
        self.data = {
            "core": {
                "listen": ["localhost", 8080],
                "url": "http://localhost:8080",
            }
        }
        self.secrets = {}
        self._modules = {}
        self._project_jobs = defaultdict(lambda:defaultdict(list))

    def load(self, filename):

        with open(filename, "rb") as cf:
            self.raw_data = yaml.safe_load(cf)

        for item in self.raw_data:
            if (not isinstance(item, dict)) and len(item.keys()) > 1:
                raise ConfigError("Invalid config entry %s" % item)
            key = list(item.keys())[0]
            value = item[key]
            name = value.get("name")
            if name:
                self.data.setdefault(key, {})
                if name in self.data[key]:
                    raise ConfigError("Duplicate %s (%s)" % (name,
                                                             self.data[key]))
                self.data[key][name] = value
            else:
                self.data.setdefault(key, {})
                self.data[key].update(value)

        for matrix in self.data.get("matrix", {}).values():
            for project in matrix["projects"]:
                for jt in ("cr", "push"):
                    for job_name in matrix.get(jt, []):
                        try:
                            job = self.data["job"][job_name]
                            job["name"] = job_name
                        except KeyError:
                            raise ConfigError("Unknown job %s" % job_name)
                        self._project_jobs[project][jt].append(job)

        #validate jobs
        for job in self.data["job"].values():
            try:
                provider = self.data["provider"][job["provider"]]
            except KeyError:
                raise ConfigError("Unknown provider %s" % job["provider"])
            try:
                cluster = provider["clusters"][job["cluster"]]
            except KeyError:
                raise ConfigError("Unknown cluster %s" % job["cluster"])
            for vm, scripts in job["scripts"].items():
                if vm not in cluster:
                    raise ConfigError("Unknown vm %s" % vm)
                for script in scripts:
                    if script not in self.data["script"]:
                        raise ConfigError("Unknown script %s" % script)

        secrets_file = self.data["core"].get("secrets")
        if secrets_file:
            self.secrets = yaml.safe_load(open(secrets_file))
        self.core = self.data["core"]

        print("Config loaded. Jobs: %s" % self._project_jobs)

    def is_project_configured(self, project):
        return project in self._project_jobs

    def get_jobs(self, project, jobs_type):
        """
        :param str project: project name
        :param str jobs_type: one of change-request, push
        :returns: list of job dicts
        """
        return self._project_jobs[project][jobs_type]

    def get_script(self, name):
        return self.data["script"].get(name, None)

    async def validate(self):
        await asyncio.sleep(0)

    def iter_instances(self, section, class_name):
        section = self.data.get(section, {})
        for config in section.values():
            module = self._get_module(config["module"])
            yield getattr(module, class_name)(self.root, **config)

    def iter_providers(self):
        for cfg in self.data.get("provider", {}).values():
            yield self._get_module(cfg["module"]).Provider(self.root, cfg)

    def _get_module(self, name):
        """Get module by name.

        Import module if it is not imported.
        """
        module = self._modules.get(name)
        if not module:
            module = importlib.import_module(name)
            self._modules[name] = module
        return module

    def configure_logging(self, verbose):
        LOGGING = {
            "version": 1,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(name)s:"
                              "%(levelname)s: %(message)s "
                              "(%(filename)s:%(lineno)d)",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "level": "DEBUG",
                    "formatter": "standard",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "": {
                    "handlers": ["console"],
                    "level": "DEBUG"
                }
            }
        }

        if verbose:
            LOGGING["handlers"]["console"]["level"] = "DEBUG"
        else:
            LOGGING["loggers"][""]["handlers"].remove("console")

        def _get_handler(key, value):
            return {
                "level": key.upper(),
                "filename": value,
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "standard"
            }

        default_log = {
            "debug": _get_handler,
            "error": _get_handler,
            "info": _get_handler,
        }

        if self.data.get("logging"):
            section = self.data.get("logging")[0]
            for key in section:
                if default_log.get(key):
                    LOGGING["handlers"][key] = default_log[key](key,
                                                                section[key])
                    LOGGING["loggers"][""]["handlers"].append(key)
                else:
                    raise ValueError("Unknown logging level")

        logging.config.dictConfig(LOGGING)
