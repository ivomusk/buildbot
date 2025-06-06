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
# Copyright 2013 (c) Mamba Team

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from dateutil.parser import parse as dateparse
from twisted.internet import defer
from twisted.python import log

from buildbot.util import bytes2unicode
from buildbot.www.hooks.base import BaseHookHandler

if TYPE_CHECKING:
    from twisted.web.server import Request

_HEADER_EVENT = b'X-Event-Key'


class BitBucketHandler(BaseHookHandler):
    def getChanges(
        self, request: Request
    ) -> defer.Deferred[tuple[list[dict[str, Any]], str | None]]:
        """Catch a POST request from BitBucket and start a build process

        Check the URL below if you require more information about payload
        https://confluence.atlassian.com/display/BITBUCKET/POST+Service+Management

        :param request: the http request Twisted object
        :param options: additional options
        """

        assert request.args is not None

        event_type = bytes2unicode(request.getHeader(_HEADER_EVENT))
        payload = json.loads(bytes2unicode(request.args[b'payload'][0]))
        repo_url = f"{payload['canon_url']}{payload['repository']['absolute_url']}"
        project = bytes2unicode(request.args.get(b'project', [b''])[0])

        changes = []
        for commit in payload['commits']:
            changes.append({
                'author': commit['raw_author'],
                'files': [f['file'] for f in commit['files']],
                'comments': commit['message'],
                'revision': commit['raw_node'],
                'when_timestamp': dateparse(commit['utctimestamp']),
                'branch': commit['branch'],
                'revlink': f"{repo_url}commits/{commit['raw_node']}",
                'repository': repo_url,
                'project': project,
                'properties': {
                    'event': event_type,
                },
            })
            log.msg(f"New revision: {commit['node']}")

        log.msg(f'Received {len(changes)} changes from bitbucket')
        return defer.succeed((changes, payload['repository']['scm']))


bitbucket = BitBucketHandler
