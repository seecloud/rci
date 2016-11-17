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
import base64
import json
import os
import re
from urllib import parse

import aiohttp


REQ_ACCESS_URL = "https://github.com/login/oauth/authorize"
REQ_TOKEN_URL = "https://github.com/login/oauth/access_token"
API_URL = "https://api.github.com/"

SAFE_NAME_RE = re.compile(r"[a-z\d][a-z\d\-]+[a-z\d]", re.IGNORECASE)

class GithubError(Exception):
    pass


def safe_bit(bit, value):
    if "--" in value:
        raise ValueError("Double dashes are not allowed.")
    if not SAFE_NAME_RE.match(value):
        raise ValueError("Value is not safe")
    return value


def format_uri(uri, args):
    r = ""
    for bit in uri.split("/"):
        if not bit:
            continue
        r += "/"
        if bit.startswith(":"):
            r += safe_bit(bit, args.pop(0))
        else:
            r += bit
    if args:
        raise ValueError("Not all arguments formatted")
    return r[1:]


class Response:
    """Wrapper class for aiohttp response"""

    def __init__(self, response):
        self._response = response

    @property
    def status(self):
        return self._response.status

    @asyncio.coroutine
    def json(self):
        return (yield from self._response.json())


class Client:
    """Represent github api client.

    Usage:
        from aiogh import github
        token = "token"  # get token somehow
        c = github.Client(token)
        data = c.get("user")
        email = data["email"]

    """

    def __init__(self, token=None, scopes=None):
        self.token = token
        self.scopes = scopes
        self.post_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.headers = {
            "Accept": "application/json",
        }
        if token:
            self.headers.update({"Authorization": "token " + token})
            self.post_headers.update({"Authorization": "token " + token})

    @asyncio.coroutine
    def post(self, uri, *args, full_response=False, **params):
        if args:
            uri = format_uri(uri, list(args))
        resp = yield from aiohttp.post(API_URL + uri,
                                       data=json.dumps(params),
                                       headers=self.post_headers)
        if 200 > resp.status > 300:
            raise GithubError(Response(resp))
        if full_response:
            return Response(resp)
        return (yield from resp.json())

    @asyncio.coroutine
    def get(self, uri, *args, full_response=False, **params):
        if args:
            uri = format_uri(uri, list(args))
        resp = yield from aiohttp.get(API_URL + uri,
                                      params=params,
                                      headers=self.headers)
        if 200 > resp.status > 300:
            raise exceptions.HttpError(Response(resp))
        if full_response:
            return Response(resp)
        return (yield from resp.json())

    @asyncio.coroutine
    def delete(self, uri, *args, full_response=False, **params):
        if args:
            uri = format_uri(uri, list(args))
        resp = yield from aiohttp.delete(API_URL + uri,
                                         params=params,
                                         headers=self.headers)
        if full_response:
            return Response(resp)
        return (yield from resp.json())


class OAuth:
    """Handle OAuth github protocol.

    Usage:
        from aiogh import github

        ...
        # somewhere in your app
        self.oauth = github.Oauth("my_client_id", "secret")
        ...

        ...
        # somewhere in web page handler
        url = self.oauth.generate_request_url(("put", "scopes", "here"))
        # make user open it
        return http_redirect(url)
        ...

        ...
        # somewhere in oauth callback handler
        code = request.GET["code"]
        state = request.GET["state"]
        client = oauth.oauth(code, state)
        # save user token if needed
        self.db.save_token(client.token)
        ...

    """

    def __init__(self, client_id, client_secret):
        """Init github oauth app.

        :param string client_id: Client ID
        :param string client_secret: Client Secret
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._requested_scopes = {}

    def generate_request_url(self, scopes: tuple):
        """Generate OAuth request url.

        :param scopes: github access scopes
                       (https://developer.github.com/v3/oauth/#scopes)
        :param callback: callback to be called when user authorize request
                         (may be coroutine)
        """
        state = base64.b64encode(os.urandom(15)).decode("ascii")
        self._requested_scopes[state] = scopes
        qs = parse.urlencode({
            "client_id": self._client_id,
            "scope": ",".join(scopes),
            "state": state,
        })
        return "%s?%s" % (REQ_ACCESS_URL, qs)

    @asyncio.coroutine
    def oauth(self, code, state):
        """Handler for Authorization callback URL.

        'code' and 'state' are GET variables given by github

        :param string code:
        :param string state:
        :return: Client instance
        :raises: exceptions.GithubException
        """
        try:
            scopes = self._requested_scopes.pop(state)
        except KeyError:
            raise exceptions.UnknownState(state)
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
            "state": state,
        }
        headers = {
            "accept": "application/json"
        }
        response = yield from aiohttp.post(REQ_TOKEN_URL,
                                           data=data,
                                           headers=headers)
        data = yield from response.json()
        return Client(data["access_token"], scopes)
