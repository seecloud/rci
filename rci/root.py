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
import logging
import signal
import resource

from rci.config import Config
from rci.http import HTTP


class Root:

    def __init__(self, loop, filename, verbose):
        self._running_objects = {}
        self._running_coros = set()
        self._running_cleanups = set()

        self._http_handlers = {}

        self.filename = filename
        self.verbose = verbose

        self.tasks = {}
        self.task_set = set()
        self.loop = loop
        self.providers = {}
        self.task_start_handlers = []
        self.task_end_handlers = []
        self.job_end_handlers = []
        self.job_update_handlers = []
        self.stop_event = asyncio.Event(loop=loop)
        self.reload_event = asyncio.Event(loop=loop)

        loop.add_signal_handler(signal.SIGINT, self.stop)
        loop.add_signal_handler(signal.SIGHUP, self.reload_event.set)

        self.task_event_map = {}
        self.eventid_event_map = {}
        self._services = []

    def notify_services(self, method_name, *args, **kwargs):
        for service in self._services:
            method = getattr(service, method_name, None)
            if method:
                try:
                    method(*args, **kwargs)
                except Exception:
                    self.log.exception("Failed to notify %s", method)

    def get_tasks(self):
        self.log.debug("ALL TASKS %s", self.task_event_map)
        return self.task_event_map.values()

    def emit(self, event):
        task = self.loop.create_task(event.run())
        self.task_event_map[task] = event
        self.eventid_event_map[event.id] = event
        task.add_done_callback(self._event_done_cb)
        self.notify_services("cb_task_started", event)

    def _event_done_cb(self, task):
        event = self.task_event_map.pop(task)
        self.notify_services("cb_task_finished", event)
        self.eventid_event_map.pop(event.id)
        self.log.info("Deleting %s", event)
        try:
            self.log.info("%s finished with %s", event, task.result())
        except Exception as ex:
            self.log.exception("Error in task %s", event)

    def stop(self):
        self.loop.remove_signal_handler(signal.SIGINT)
        self.loop.remove_signal_handler(signal.SIGHUP)
        self.stop_event.set()

    @asyncio.coroutine
    def run_obj(self, obj):
        try:
            yield from self.run_coro(obj.run())
        except Exception:
            self.log.exception("Error running %s" % obj)

    @asyncio.coroutine
    def run_coro(self, coro):
        try:
            yield from coro
        except asyncio.CancelledError:
            self.log.info("Cancelled %s" % coro)
        except Exception:
            self.log.exception("Exception running %s" % coro)

    def _task_cb(self, task):
        try:
            result = task.result()
        except Exception:
            self.log.exception("Error in task %s", task)
        self.log.info("Task finished %s", task)

    def start_coro(self, coro):
        task = self.loop.create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(self._task_cb)

    def task_done_cb(self, fut):
        try:
            task = self.tasks.pop(fut)
            self.log.info("Finished task %s" % task)
            self.task_set.remove(task.event.head)
            for handler in self.task_end_handlers:
                try:
                    handler(task)
                except Exception:
                    self.log.exception(("Exception in task end "
                                        "handler %s %s") % (task, handler))
        except Exception:
            self.log.exception("Error in task_done_cb")

    def start_coro(self, coro):
        fut = asyncio.async(self.run_coro(coro), loop=self.loop)
        self._running_coros.add(fut)
        fut.add_done_callback(self._running_coros.remove)
        return fut

    def start_obj(self, obj):
        fut = asyncio.async(self.run_obj(obj), loop=self.loop)
        self._running_objects[fut] = obj
        if hasattr(obj, "cleanup"):
            fut.add_done_callback(self.schedule_cleanup)
        else:
            fut.add_done_callback(self._running_objects.pop)
        return fut

    @asyncio.coroutine
    def run_cleanup(self, obj):
        try:
            yield from obj.cleanup()
        except Exception:
            self.log.exception("Exception in cleanup %s" % obj)

    def schedule_cleanup(self, fut):
        obj = self._running_objects.pop(fut)
        fut = asyncio.async(self.run_cleanup(obj), loop=self.loop)
        self._running_cleanups.add(fut)
        fut.add_done_callback(self._running_cleanups.remove)

    def _load_config(self):
        self.config = Config(self)
        self.config.load(self.filename)
        self.config.configure_logging(self.verbose)
        self.log = logging.getLogger(__name__)

    @asyncio.coroutine
    def wait_fs(self, fs):
        """Wait for futures.

        :param fs: dict where key is future and value is related object
        """
        self.log.debug("Waiting for %s" % fs.values())
        while fs:
            done, pending = yield from asyncio.wait(
                    list(fs.keys()), return_when=futures.FIRST_COMPLETED)
            for fut in done:
                if fut in fs:
                    del(fs[fut])
            self.log.debug("Pending %s" % pending)

    @asyncio.coroutine
    def reload(self):
        """Wait for reload events and reload config when event set."""
        while 1:
            try:
                yield from self.reload_event.wait()
                self.log.info("Reloading configuration...")
            except asyncio.CancelledError:
                return
            finally:
                self.reload_event.clear()
            try:
                config = Config(self, self.filename, self.verbose)
                self.log.debug("New config instance %s" % config)
                yield from config.validate()
                self.config = config
                self.log.info("Done")
            except asyncio.CancelledError:
                return
            except Exception:
                self.log.exception("Error loading new config")

    @asyncio.coroutine
    def _http_handler(self, request):
        print(request.path)
        handler = self._http_handlers.get(request.path.split("/")[1])
        if handler:
            response = yield from handler(request)
            return response

    def _start_service(self, service):
        self._services.append(service)
        self.start_obj(service)
        http_path = getattr(service, "http_path", None)
        if http_path:
            self._http_handlers[http_path] = service.http_handler

    @asyncio.coroutine
    def run(self):
        self._load_config()

        for service in self.config.iter_instances("service", "Service"):
            self._start_service(service)

        listen = self.config.raw_data[0]["core"]["listen"]

        self.http = HTTP(self.loop, listen)
        self.http.add_route("*", r"/{dir:.*}", self._http_handler)

        for prov in self.config.iter_providers():
            self.providers[prov.name] = prov
            yield from prov.start()
        yield from self.http.start()
        reload_fut = asyncio.async(self.reload(), loop=self.loop)
        yield from self.stop_event.wait()
        self.log.info("Interrupted.")
        reload_fut.cancel()
        yield from reload_fut
        for obj in self._running_objects:
            obj.cancel()
        yield from asyncio.wait(self._running_objects,
                                return_when=futures.ALL_COMPLETED)
        if self._running_cleanups:
            yield from asyncio.wait(self._running_cleanups,
                                    return_when=futures.ALL_COMPLETED)
        for provider in self.providers.values():
            yield from prov.stop()
        self.http.stop()
        yield from self.http.wait_closed()
        self.log.info("Exit.")

    def job_updated(self, job):
        for cb in self.job_update_handlers:
            cb(job)

    def get_daemon_statistics(self):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return {"type": "daemon-statistics",
                "memory-used": getattr(usage, "ru_maxrss")}
