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

from typing import TYPE_CHECKING
from typing import ClassVar

from twisted.internet import defer
from zope.interface import implementer

from buildbot import interfaces
from buildbot.process.results import statusToString
from buildbot.reporters import utils
from buildbot.reporters.message import MessageFormatter

from .utils import BuildStatusGeneratorMixin

if TYPE_CHECKING:
    from collections.abc import Sequence


@implementer(interfaces.IReportGenerator)
class BuildSetStatusGenerator(BuildStatusGeneratorMixin):
    wanted_event_keys = [
        ('buildsets', None, 'complete'),
    ]

    compare_attrs: ClassVar[Sequence[str]] = ['formatter']

    def __init__(
        self,
        mode=("failing", "passing", "warnings"),
        tags=None,
        builders=None,
        schedulers=None,
        branches=None,
        subject=None,
        add_logs=None,
        add_patch=False,
        message_formatter=None,
    ):
        subject = "Buildbot %(result)s in %(title)s on %(builder)s"
        super().__init__(mode, tags, builders, schedulers, branches, subject, add_logs, add_patch)
        self.formatter = message_formatter
        if self.formatter is None:
            self.formatter = MessageFormatter()

    @defer.inlineCallbacks
    def generate(self, master, reporter, key, message):
        bsid = message['bsid']
        res = yield utils.getDetailsForBuildset(
            master,
            bsid,
            want_properties=self.formatter.want_properties,
            want_steps=self.formatter.want_steps,
            want_previous_build=self._want_previous_build(),
            want_logs=self.formatter.want_logs,
            want_logs_content=self.formatter.want_logs_content,
        )

        builds = res['builds']
        buildset = res['buildset']

        # only include builds for which isMessageNeeded returns true
        builds = [
            build
            for build in builds
            if self.is_message_needed_by_props(build) and self.is_message_needed_by_results(build)
        ]
        if not builds:
            return None

        report = yield self.buildset_message(self.formatter, master, reporter, builds, buildset)
        return report

    @defer.inlineCallbacks
    def buildset_message(self, formatter, master, reporter, builds, buildset):
        # The given builds must refer to builds from a single buildset
        patches = []
        logs = []
        body = None
        subject = None
        msgtype = None
        extra_info = None
        users = set()
        results = buildset["results"]
        for build in builds:
            patches.extend(self._get_patches_for_build(build))

            build_logs = yield self._get_logs_for_build(build)
            logs.extend(build_logs)

            blamelist = yield reporter.getResponsibleUsersForBuild(master, build['buildid'])
            users.update(set(blamelist))

            buildmsg = yield formatter.format_message_for_build(
                master, build, is_buildset=True, mode=self.mode, users=blamelist
            )

            msgtype, ok = self._merge_msgtype(msgtype, buildmsg['type'])
            if not ok:
                continue

            subject = self._merge_subject(subject, buildmsg['subject'])

            body, ok = self._merge_body(body, buildmsg['body'])
            if not ok:
                continue

            extra_info, ok = self._merge_extra_info(extra_info, buildmsg["extra_info"])
            if not ok:
                continue

        if subject is None and self.subject is not None:
            subject = self.subject % {
                'result': statusToString(results),
                'projectName': master.config.title,
                'title': master.config.title,
                'builder': 'whole buildset',
            }

        return {
            'body': body,
            'subject': subject,
            'type': msgtype,
            'results': results,
            'builds': builds,
            "buildset": buildset,
            'users': list(users),
            'patches': patches,
            'logs': logs,
            "extra_info": extra_info,
        }

    def _want_previous_build(self):
        return "change" in self.mode or "problem" in self.mode


@implementer(interfaces.IReportGenerator)
class BuildSetCombinedStatusGenerator:
    wanted_event_keys = [
        ("buildsets", None, "complete"),
    ]

    compare_attrs: ClassVar[Sequence[str]] = ["formatter"]

    def __init__(self, message_formatter):
        self.formatter = message_formatter

    @defer.inlineCallbacks
    def generate(self, master, reporter, key, message):
        bsid = message["bsid"]

        res = yield utils.getDetailsForBuildset(
            master,
            bsid,
            want_properties=self.formatter.want_properties,
            want_steps=self.formatter.want_steps,
            want_logs=self.formatter.want_logs,
            want_logs_content=self.formatter.want_logs_content,
        )

        builds = res['builds']
        buildset = res['buildset']

        report = yield self.buildset_message(self.formatter, master, reporter, buildset, builds)

        return report

    def check(self):
        pass

    @defer.inlineCallbacks
    def buildset_message(self, formatter, master, reporter, buildset, builds):
        buildmsg = yield formatter.format_message_for_buildset(
            master, buildset, builds, is_buildset=True, mode=("passing",), users=[]
        )

        return {
            "body": buildmsg["body"],
            "subject": buildmsg["subject"],
            "type": buildmsg["type"],
            "extra_info": buildmsg["extra_info"],
            "results": buildset["results"],
            "builds": builds,
            "buildset": buildset,
            "users": [],
            "patches": [],
            "logs": [],
        }
