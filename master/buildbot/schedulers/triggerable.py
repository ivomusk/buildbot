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
from typing import Any
from typing import ClassVar

from twisted.internet import defer
from twisted.python import failure
from zope.interface import implementer

from buildbot.interfaces import ITriggerableScheduler
from buildbot.process.properties import Properties
from buildbot.schedulers import base
from buildbot.util import debounce

if TYPE_CHECKING:
    from collections.abc import Sequence


@implementer(ITriggerableScheduler)
class Triggerable(base.ReconfigurableBaseScheduler):
    compare_attrs: ClassVar[Sequence[str]] = (
        *base.ReconfigurableBaseScheduler.compare_attrs,
        'reason',
    )

    def __init__(self, name, builderNames, *args, **kwargs):
        super().__init__(*args, name=name, builderNames=builderNames, **kwargs)
        self._waiters = {}
        self._buildset_complete_consumer = None

    def checkConfig(self, builderNames, reason=None, **kwargs: Any):  # type: ignore[override]
        super().checkConfig(builderNames=builderNames, **kwargs)

    @defer.inlineCallbacks
    def reconfigService(  # type: ignore[override]
        self,
        builderNames,
        reason=None,
        **kwargs: Any,
    ):
        yield super().reconfigService(builderNames=builderNames, **kwargs)
        self.reason = reason

    def trigger(
        self,
        waited_for,
        sourcestamps=None,
        set_props=None,
        parent_buildid=None,
        parent_relationship=None,
    ):
        """Trigger this scheduler with the optional given list of sourcestamps
        Returns two deferreds:
            idsDeferred -- yields the ids of the buildset and buildrequest, as soon as they are
            available.
            resultsDeferred -- yields the build result(s), when they finish."""
        # properties for this buildset are composed of our own properties,
        # potentially overridden by anything from the triggering build
        props = Properties()
        props.updateFromProperties(self.properties)

        reason = self.reason
        if set_props:
            props.updateFromProperties(set_props)
            reason = set_props.getProperty('reason')

        if reason is None:
            reason = f"The Triggerable scheduler named '{self.name}' triggered this build"

        # note that this does not use the buildset subscriptions mechanism, as
        # the duration of interest to the caller is bounded by the lifetime of
        # this process.
        idsDeferred = self.addBuildsetForSourceStampsWithDefaults(
            reason,
            sourcestamps,
            waited_for,
            priority=self.priority,
            properties=props,
            parent_buildid=parent_buildid,
            parent_relationship=parent_relationship,
        )

        resultsDeferred = defer.Deferred()

        @idsDeferred.addCallback
        def setup_waiter(ids):
            bsid, brids = ids
            self._waiters[bsid] = (resultsDeferred, brids)
            self._updateWaiters()
            return ids

        return idsDeferred, resultsDeferred

    @defer.inlineCallbacks
    def startService(self):
        yield super().startService()
        self._updateWaiters.start()

    @defer.inlineCallbacks
    def stopService(self):
        # finish any _updateWaiters calls
        yield self._updateWaiters.stop()

        # cancel any outstanding subscription
        if self._buildset_complete_consumer:
            self._buildset_complete_consumer.stopConsuming()
            self._buildset_complete_consumer = None

        # and errback any outstanding deferreds
        if self._waiters:
            msg = 'Triggerable scheduler stopped before build was complete'
            for d, _ in self._waiters.values():
                d.errback(failure.Failure(RuntimeError(msg)))
            self._waiters = {}

        yield super().stopService()

    @debounce.method(wait=0)
    @defer.inlineCallbacks
    def _updateWaiters(self):
        if self._waiters and not self._buildset_complete_consumer:
            startConsuming = self.master.mq.startConsuming
            self._buildset_complete_consumer = yield startConsuming(
                self._buildset_complete_cb, ('buildsets', None, 'complete')
            )
        elif not self._waiters and self._buildset_complete_consumer:
            self._buildset_complete_consumer.stopConsuming()
            self._buildset_complete_consumer = None

    def _buildset_complete_cb(self, key, msg):
        if msg['bsid'] not in self._waiters:
            return

        # pop this bsid from the waiters list,
        d, brids = self._waiters.pop(msg['bsid'])
        # ..and potentially stop consuming buildset completion notifications
        self._updateWaiters()

        # fire the callback to indicate that the triggered build is complete
        d.callback((msg['results'], brids))
