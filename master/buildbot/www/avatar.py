# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlparse
from urllib.parse import urlunparse

from twisted.internet import defer
from twisted.python import log

from buildbot import config
from buildbot.util import bytes2unicode
from buildbot.util import httpclientservice
from buildbot.util import unicode2bytes
from buildbot.util.config import ConfiguredMixin
from buildbot.www import resource

if TYPE_CHECKING:
    from buildbot.master import BuildMaster
    from buildbot.util.twisted import InlineCallbacksType


class AvatarBase(ConfiguredMixin):
    name = "noavatar"

    master: BuildMaster | None = None

    def getUserAvatar(
        self, email: bytes, username: bytes | None, size: int, defaultAvatarUrl: str
    ) -> Any:
        raise NotImplementedError()


class AvatarGitHub(AvatarBase):
    name = "github"

    DEFAULT_GITHUB_API_URL = 'https://api.github.com'

    client: httpclientservice.HTTPSession | None = None

    def __init__(
        self,
        github_api_endpoint: str | None = None,
        token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        debug: bool = False,
        verify: bool = True,
    ) -> None:
        self.github_api_endpoint = github_api_endpoint
        if github_api_endpoint is None:
            self.github_api_endpoint = self.DEFAULT_GITHUB_API_URL
        self.token = token
        self.client_creds = None
        if bool(client_id) != bool(client_secret):
            config.error('client_id and client_secret must be both provided or none')
        if client_id:
            if token:
                config.error('client_id and client_secret must not be provided when token is')
            # We know client_id and client_secret are not None here because of the check above
            self.client_creds = base64.b64encode(
                b':'.join(cred.encode('utf-8') for cred in (client_id, client_secret))  # type: ignore[union-attr]
            ).decode('ascii')
        self.debug = debug
        self.verify = verify

        self.master = None
        self.client = None

    @defer.inlineCallbacks
    def _get_http_client(self) -> InlineCallbacksType[httpclientservice.HTTPSession]:
        assert self.master is not None

        if self.client is not None:
            return self.client

        headers = {
            'User-Agent': 'Buildbot',
        }
        if self.token:
            headers['Authorization'] = 'token ' + self.token
        elif self.client_creds:
            headers['Authorization'] = 'basic ' + self.client_creds

        self.client = yield httpclientservice.HTTPSession(
            self.master.httpservice,
            self.github_api_endpoint,
            headers=headers,
            debug=self.debug,
            verify=self.verify,
        )

        return self.client

    @defer.inlineCallbacks
    def _get_avatar_by_username(self, username: str) -> InlineCallbacksType[str | None]:
        headers = {
            'Accept': 'application/vnd.github.v3+json',
        }

        url = f'/users/{username}'
        http = yield self._get_http_client()
        res = yield http.get(url, headers=headers)
        if res.code == 404:
            # Not found
            return None
        if 200 <= res.code < 300:
            data = yield res.json()
            return data['avatar_url']

        log.msg(f'Failed looking up user: response code {res.code}')
        return None

    @defer.inlineCallbacks
    def _search_avatar_by_user_email(self, email: str) -> InlineCallbacksType[str | None]:
        headers = {
            'Accept': 'application/vnd.github.v3+json',
        }

        query = f'{email} in:email'
        url = f"/search/users?{urlencode({'q': query})}"
        http = yield self._get_http_client()
        res = yield http.get(url, headers=headers)
        if 200 <= res.code < 300:
            data = yield res.json()
            if data['total_count'] == 0:
                # Not found
                return None
            return data['items'][0]['avatar_url']

        log.msg(f'Failed searching user by email: response code {res.code}')
        return None

    @defer.inlineCallbacks
    def _search_avatar_by_commit(self, email: str) -> InlineCallbacksType[str | None]:
        headers = {
            'Accept': 'application/vnd.github.v3+json,application/vnd.github.cloak-preview',
        }

        query = {
            'q': f'author-email:{email}',
            'sort': 'committer-date',
            'per_page': '1',
        }
        sorted_query = sorted(query.items(), key=lambda x: x[0])
        url = f'/search/commits?{urlencode(sorted_query)}'
        http = yield self._get_http_client()
        res = yield http.get(url, headers=headers)
        if 200 <= res.code < 300:
            data = yield res.json()
            if data['total_count'] == 0:
                # Not found
                return None
            author = data['items'][0]['author']
            if author is None:
                # No Github account found
                return None
            return author['avatar_url']

        log.msg(f'Failed searching user by commit: response code {res.code}')
        return None

    def _add_size_to_url(self, avatar: str, size: int) -> str:
        parts = urlparse(avatar)
        query = parts.query
        if query:
            query += '&'
        query += f's={size}'
        return urlunparse((
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.params,
            query,
            parts.fragment,
        ))

    @defer.inlineCallbacks
    def getUserAvatar(
        self, email: bytes, username: bytes | None, size: int, defaultAvatarUrl: str
    ) -> InlineCallbacksType[Any | None]:
        username_str = username.decode('utf-8') if username else None
        email_str = email.decode('utf-8') if email else None

        avatar: str | None = None
        if username_str:
            avatar = yield self._get_avatar_by_username(username_str)
        if not avatar and email_str:
            avatar = yield self._search_avatar_by_user_email(email_str)
        if not avatar and email_str:
            # No luck, try to find a commit with this email
            avatar = yield self._search_avatar_by_commit(email_str)

        if not avatar:
            # No luck
            return None

        if size:
            avatar = self._add_size_to_url(avatar, size)

        raise resource.Redirect(avatar)


class AvatarGravatar(AvatarBase):
    name = "gravatar"
    # gravatar does not want intranet URL, which is most of where the bots are
    # just use same default as github (retro)
    default = "retro"

    def getUserAvatar(
        self, email: bytes, username: bytes | None, size: int, defaultAvatarUrl: str
    ) -> Any:
        # construct the url
        emailBytes = unicode2bytes(email.lower())
        emailHash = hashlib.md5(emailBytes)
        gravatar_url = "//www.gravatar.com/avatar/"
        gravatar_url += emailHash.hexdigest() + "?"
        if self.default != "url":
            defaultAvatarUrl = self.default
        url = {'d': defaultAvatarUrl, 's': str(size)}
        sorted_url = sorted(url.items(), key=lambda x: x[0])
        gravatar_url += urlencode(sorted_url)
        raise resource.Redirect(gravatar_url)


class AvatarResource(resource.Resource):
    # enable reconfigResource calls
    needsReconfig = True
    defaultAvatarUrl = b"img/nobody.png"

    avatarMethods: list[AvatarBase] = []
    defaultAvatarFullUrl: bytes
    cache: dict[tuple[bytes, bytes | None, int], Any]

    def reconfigResource(self, new_config: Any) -> None:
        avatar_methods = new_config.www.get('avatar_methods', [])
        self.defaultAvatarFullUrl = urljoin(
            unicode2bytes(new_config.buildbotURL), unicode2bytes(self.defaultAvatarUrl)
        )
        self.cache = {}

        # ensure the avatarMethods is a iterable
        if isinstance(avatar_methods, AvatarBase):
            self.avatarMethods = [avatar_methods]
        else:
            self.avatarMethods = avatar_methods

        for method in self.avatarMethods:
            method.master = self.master

    def render_GET(self, request: Any) -> Any:
        return self.asyncRenderHelper(request, self.renderAvatar)

    @defer.inlineCallbacks
    def renderAvatar(self, request: Any) -> InlineCallbacksType[Any]:
        email = request.args.get(b"email", [b""])[0]
        size = request.args.get(b"size", [32])[0]
        try:
            size = int(size)
        except ValueError:
            size = 32
        username = request.args.get(b"username", [None])[0]
        cache_key = (email, username, size)
        if self.cache.get(cache_key):
            raise self.cache[cache_key]
        for method in self.avatarMethods:
            try:
                res = yield method.getUserAvatar(
                    email, username, size, bytes2unicode(self.defaultAvatarFullUrl)
                )
            except resource.Redirect as r:
                self.cache[cache_key] = r
                raise
            if res is not None:
                request.setHeader(b'content-type', res[0])
                request.setHeader(b'content-length', unicode2bytes(str(len(res[1]))))
                request.write(res[1])
                return
        raise resource.Redirect(self.defaultAvatarUrl)
