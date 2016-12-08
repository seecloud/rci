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
import unittest
from unittest import mock

import aiohttp

from rci.services.github import service


async def mock_coro(retval):
    return retval


class GithubTestCase(unittest.TestCase):

    @mock.patch("rci.services.github.service.github")
    @mock.patch("rci.services.github.service.job")
    def test_github(self, mock_job, mock_github):
        mock_github.OAuth.return_value.generate_request_url.return_value = "ok"
        loop = asyncio.get_event_loop()
        root = mock.Mock(loop=loop)
        root.config.secrets = {"gh": {"id": "fake"}}
        root.config.get_jobs.return_value = [2,3,4]
        cfg = {
            "name": "gh",
            "data-path": "/tmp/dm",
        }
        s = service.Service(root, **cfg)
        s.orgs = {b"42": b"org-token-42"}
        run = loop.create_task(s.run())

        # test login
        req = mock.Mock(path = "/github/login")
        resp = loop.run_until_complete(s.http_handler(req))
        self.assertIsInstance(resp, aiohttp.web.HTTPFound)
        self.assertEqual("ok", resp.location)

        # test webhook
        req = mock.Mock(path = "/github/webhook")
        req.headers = {"X-Github-Event": "pull_request"}
        fake_pr = {
            "action": "opened",
            "pull_request": {
                "title": "Test PR",
                "url": "fake-url",
                "head": {
                    "sha": "fake-sha",
                    "repo": {
                        "full_name": "test/fork",
                        "clone_url": "fake-clone-url",
                    },
                },
            },
            "repository": {
                "full_name": "test/repo",
                "owner": {
                    "type": "Organization",
                    "id": "42",
                },
            },
        }
        req.json.return_value = mock_coro(fake_pr)
        resp = loop.run_until_complete(s.http_handler(req))
        self.assertEqual("ok", resp.text)
        event = root.emit.call_args[0][0]
        self.assertIsInstance(event, service.Event)

        run.cancel()
        self.assertRaises(asyncio.CancelledError, loop.run_until_complete, run)
        loop.close()
