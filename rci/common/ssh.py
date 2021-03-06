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
import logging
import os
import pwd
import subprocess
import time

import asyncssh
from rci.utils import LogDel

LOG = logging.getLogger(__name__)


class SSHError(Exception):
    pass


class SSHProcessFailed(SSHError):
    pass


class SSHProcessKilled(SSHProcessFailed):
    pass


class SSHClient(asyncssh.SSHClient, LogDel):

    def __init__(self, ssh, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ssh = ssh

    def connection_made(self, conn):
        self._ssh._connected.set()
        self._ssh.closed.clear()

    def connection_lost(self, ex):
        self._ssh.closed.set()
        self._ssh._connected.clear()
        self._ssh.client = None
        self._ssh.conn = None
        self._ssh = None


class SSHClientSession(asyncssh.SSHClientSession, LogDel):

    def __init__(self, callbacks, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stdout_cb, self._stderr_cb = callbacks

    def data_received(self, data, datatype):
        if self._stdout_cb and datatype is None:
            self._stdout_cb(data)
        elif self._stderr_cb and (datatype == asyncssh.EXTENDED_DATA_STDERR):
            self._stderr_cb(data)

    def connection_lost(self, ex):
        self._stdout_cb = None
        self._stderr_cb = None


class SSH(LogDel):

    def __init__(self, loop, hostname=None, username=None, keys=None, port=22,
                 cb=None, jumphost=None, password=None):
        """
        :param SSH jumphost:
        """
        self.loop = loop
        self.username = username or pwd.getpwuid(os.getuid()).pw_name
        self.hostname = hostname
        self.password = password
        self.port = port
        self.cb = cb
        self.jumphost = jumphost

        self._forwarded_remote_ports = []
        if keys:
            self.keys = []
            for key in keys:
                if key.startswith("~"):
                    key = os.path.expanduser(key)
                self.keys.append(key)
        else:
            self.keys = None
        self._connecting = asyncio.Lock(loop=loop)
        self._connected = asyncio.Event(loop=loop)
        self.closed = asyncio.Event(loop=loop)
        self.closed.set()

    def __repr__(self):
        return "<SSH %s@%s>" % (self.username, self.hostname)

    def client_factory(self, *args, **kwargs):
        return SSHClient(self, *args, **kwargs)

    def close(self):
        if hasattr(self, "conn"):
            if self._connected.is_set():
                self.conn.close()

    @asyncio.coroutine
    def wait_closed(self):
        yield from self.closed.wait()

    @asyncio.coroutine
    def _ensure_connected(self, forward_remote_port=None):
        with (yield from self._connecting):
            if forward_remote_port:
                self._forwarded_remote_ports.append(forward_remote_port)
            if self.jumphost:
                yield from self.jumphost._ensure_connected()
                tunnel = self.jumphost.conn
            else:
                tunnel = None
            if self._connected.is_set():
                if forward_remote_port:
                    args, kwargs = forward_remote_port
                    listener = yield from self.conn.\
                            forward_remote_port(*args, **kwargs)
                    # TODO: cancel it when client closed
                    self.loop.create_task(listener.wait_closed())
                return
            LOG.debug("Connecting %s@%s (keys %s) (via %s)" % (self.username,
                                                               self.hostname,
                                                               self.keys,
                                                               self.jumphost))
            self.conn, self.client = yield from asyncssh.create_connection(
                functools.partial(SSHClient, self), self.hostname,
                username=self.username, known_hosts=None,
                password=self.password,
                client_keys=self.keys, port=self.port, tunnel=tunnel)
            for args, kwargs in self._forwarded_remote_ports:
                try:
                    yield from self.conn.forward_remote_port(*args, **kwargs)
                except Exception as ex:
                    print(ex)
            LOG.debug("Connected %s@%s" % (self.username, self.hostname))

    @asyncio.coroutine
    def forward_remote_port(self, *args, **kwargs):
        yield from self._ensure_connected(forward_remote_port=(args, kwargs))

    @asyncio.coroutine
    def run(self, cmd, stdin=None, stdout=None, stderr=None, check=True,
            env=None):
        """Run command on remote server.

        :param string cmd: command to be executed
        :param stdin: either string, bytes or file like object
        :param stdout: executable (e.g. sys.stdout.write)
        :param stderr: executable (e.g. sys.stderr.write)
        :param boolean check: Raise exception if non-zero exit status.
        :returns: exit status (if check==Fasle or status is zero)
        """
        if isinstance(cmd, list):
            cmd = _escape_cmd(cmd)
        cmd = _escape_env(env) + cmd
        LOG.debug("Running %s" % cmd)
        yield from self._ensure_connected()
        session_factory = functools.partial(SSHClientSession, (stdout, stderr))
        chan, session = yield from self.conn.create_session(session_factory,
                                                            cmd, env=env or {})
        if stdin:
            if hasattr(stdin, "read"):
                while True:
                    chunk = stdin.read(4096)
                    if hasattr(chunk, "decode"):
                        chunk = chunk.decode("ascii")  # TODO
                    if not chunk:
                        break
                    chan.write(chunk)
                    # TODO: drain
            else:
                chan.write(stdin)
            chan.write_eof()
        yield from chan.wait_closed()
        status = chan.get_exit_status()
        if check and status == -1:
            raise SSHProcessKilled(chan.get_exit_signal())
        if check and status != 0:
            raise SSHProcessFailed(status)
        return status

    @asyncio.coroutine
    def out(self, cmd, stdin=None, check=True, env=None):
        stdout = []
        stderr = []
        status = yield from self.run(cmd, stdout=stdout.append,
                                     stderr=stderr.append,
                                     stdin=stdin, check=check, env=env)
        stdout = "".join(stdout)
        stderr = "".join(stderr)
        return (status, stdout, stderr)

    @asyncio.coroutine
    def wait(self, timeout=180, delay=2):
        _start = time.time()
        while True:
            try:
                yield from self._ensure_connected()
                return
            except Exception as ex:
                LOG.debug("Waiting ssh %s: %s" % (self, ex))
            if time.time() > (_start + timeout):
                raise SSHError("timeout %s:%s" % (self.hostname, self.port))
            yield from asyncio.sleep(delay)

    @asyncio.coroutine
    def get(self, *args, **kwargs):
        LOG.debug("SCP %s %s %s" % (args, kwargs, self))
        yield from self._ensure_connected()
        with (yield from self.conn.start_sftp_client()) as sftp:
            yield from sftp.get(*args, **kwargs)
        LOG.debug("DONE SCP %s %s %s" % (args, kwargs, self))

    @asyncio.coroutine
    def scp_get(self, src, dst):
        LOG.debug("SCP %s %s %s" % (src, dst, self))
        cmd = ["scp", "-r", "-B", "-o", "StrictHostKeyChecking no",
               "-o", "UserKnownHostsFile /dev/null"]
        for key in (self.keys or []):
            cmd += ["-i", key]
        cmd += ["-P", str(self.port)]
        cmd += ["-r", "%s@%s:%s" % (self.username, self.hostname, src), dst]
        LOG.debug("Runnung %s" % " ".join(cmd))
        process = asyncio.create_subprocess_exec(*cmd,
                                                 stdout=subprocess.PIPE,
                                                 stderr=subprocess.STDOUT)
        process = yield from process
        try:
            while not process.stdout.at_eof():
                line = yield from process.stdout.read()
                LOG.debug("scp: %s" % line)
        except asyncio.CancelledError:
            process.terminate()
            asyncio.async(process.wait(), loop=asyncio.get_event_loop())
            raise
        LOG.debug("DONE SCP %s %s %s" % (src, dst, self))
        return process.returncode


def _escape(string):
    return string.replace(r"'", r"'\''")


def _escape_env(env):
    cmd = ""
    if env:
        for key, val in env.items():
            # TODO: safe variable name
            cmd += "%s='%s' " % (key, _escape(str(val)))
    return cmd


def _escape_cmd(cmd):
    return " ".join(["'%s'" % _escape(arg) for arg in cmd])
