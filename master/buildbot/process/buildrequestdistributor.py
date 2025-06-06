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

import copy
import math
import random
from datetime import datetime
from typing import TYPE_CHECKING

from twisted.internet import defer
from twisted.python import log
from twisted.python.failure import Failure

from buildbot.data import resultspec
from buildbot.db.buildrequests import BuildRequestModel
from buildbot.process import metrics
from buildbot.process.buildrequest import BuildRequest
from buildbot.util import deferwaiter
from buildbot.util import epoch2datetime
from buildbot.util import service
from buildbot.util.async_sort import async_sort
from buildbot.util.twisted import async_to_deferred

if TYPE_CHECKING:
    from buildbot.process.builder import Builder


class BuildChooserBase:
    #
    # WARNING: This API is experimental and in active development.
    #
    # This internal object selects a new build+worker pair. It acts as a
    # generator, initializing its state on creation and offering up new
    # pairs until exhaustion. The object can be destroyed at any time
    # (eg, before the list exhausts), and can be "restarted" by abandoning
    # an old instance and creating a new one.
    #
    # The entry point is:
    #    * bc.chooseNextBuild() - get the next (worker, [breqs]) or
    #      (None, None)
    #
    # The default implementation of this class implements a default
    # chooseNextBuild() that delegates out to two other functions:
    #   * bc.popNextBuild() - get the next (worker, breq) pair

    def __init__(self, bldr, master):
        self.bldr = bldr
        self.master = master
        self.breqCache = {}
        self.unclaimedBrdicts = None

    @defer.inlineCallbacks
    def chooseNextBuild(self):
        # Return the next build, as a (worker, [breqs]) pair

        worker, breq = yield self.popNextBuild()
        if not worker or not breq:
            return (None, None)

        return (worker, [breq])

    # Must be implemented by subclass
    def popNextBuild(self):
        # Pick the next (worker, breq) pair; note this is pre-merge, so
        # it's just one breq
        raise NotImplementedError("Subclasses must implement this!")

    # - Helper functions that are generally useful to all subclasses -
    @defer.inlineCallbacks
    def _fetchUnclaimedBrdicts(self):
        # Sets up a cache of all the unclaimed brdicts. The cache is
        # saved at self.unclaimedBrdicts cache. If the cache already
        # exists, this function does nothing. If a refetch is desired, set
        # the self.unclaimedBrdicts to None before calling."""
        if self.unclaimedBrdicts is None:
            # TODO: use order of the DATA API
            brdicts = yield self.master.data.get(
                ('builders', (yield self.bldr.getBuilderId()), 'buildrequests'),
                [resultspec.Filter('claimed', 'eq', [False])],
            )
            # sort by buildrequestid, so the first is the oldest
            brdicts.sort(key=lambda brd: brd['buildrequestid'])
            self.unclaimedBrdicts = brdicts
        return self.unclaimedBrdicts

    @defer.inlineCallbacks
    def _getBuildRequestForBrdict(self, brdict: dict):
        # Turn a brdict into a BuildRequest into a brdict. This is useful
        # for API like 'nextBuild', which operate on BuildRequest objects.

        breq = self.breqCache.get(brdict['buildrequestid'])
        if not breq:
            builder = yield self.master.data.get(
                ('builders', brdict['builderid']), [resultspec.ResultSpec(fields=['name'])]
            )
            if not builder:
                return None

            model = BuildRequestModel(
                buildrequestid=brdict['buildrequestid'],
                buildsetid=brdict['buildsetid'],
                builderid=brdict['builderid'],
                buildername=builder['name'],
                submitted_at=brdict['submitted_at'],
            )
            if 'complete_at' in brdict:
                model.complete_at = brdict['complete_at']
            if 'complete' in brdict:
                model.complete = brdict['complete']
            if 'results' in brdict:
                model.results = brdict['results']
            if 'waited_for' in brdict:
                model.waited_for = brdict['waited_for']
            if 'priority' in brdict:
                model.priority = brdict['priority']
            if 'claimed_at' in brdict:
                model.claimed_at = brdict['claimed_at']
            if 'claimed_by_masterid' in brdict:
                model.claimed_by_masterid = brdict['claimed_by_masterid']

            breq = yield BuildRequest.fromBrdict(self.master, model)
            if breq:
                self.breqCache[model.buildrequestid] = breq
        return breq

    def _getBrdictForBuildRequest(self, breq):
        # Turn a BuildRequest back into a brdict. This operates from the
        # cache, which must be set up once via _fetchUnclaimedBrdicts

        if breq is None:
            return None

        brid = breq.id
        for brdict in self.unclaimedBrdicts:
            if brid == brdict['buildrequestid']:
                return brdict
        return None

    def _removeBuildRequest(self, breq):
        # Remove a BuildrRequest object (and its brdict)
        # from the caches

        if breq is None:
            return

        brdict = self._getBrdictForBuildRequest(breq)
        if brdict is not None:
            self.unclaimedBrdicts.remove(brdict)

        if breq.id in self.breqCache:
            del self.breqCache[breq.id]

    def _getUnclaimedBuildRequests(self):
        # Retrieve the list of BuildRequest objects for all unclaimed builds
        return defer.gatherResults(
            [self._getBuildRequestForBrdict(brdict) for brdict in self.unclaimedBrdicts],
            consumeErrors=True,
        )


class BasicBuildChooser(BuildChooserBase):
    # BasicBuildChooser generates build pairs via the configuration points:
    #   * config.nextWorker  (or random.choice if not set)
    #   * config.nextBuild  (or "pop top" if not set)
    #
    # For N workers, this will call nextWorker at most N times. If nextWorker
    # returns a worker that cannot satisfy the build chosen by nextBuild,
    # it will search for a worker that can satisfy the build. If one is found,
    # the workers that cannot be used are "recycled" back into a list
    # to be tried, in order, for the next chosen build.
    #
    # We check whether Builder.canStartBuild returns True for a particular
    # worker. It evaluates any Build properties that are known before build
    # and checks whether the worker may satisfy them. For example, the worker
    # must have the locks available.

    def __init__(self, bldr, master):
        super().__init__(bldr, master)

        self.nextWorker = self.bldr.config.nextWorker
        if not self.nextWorker:
            self.nextWorker = self.master.config.select_next_worker
        if not self.nextWorker:
            self.nextWorker = lambda _, workers, __: random.choice(workers) if workers else None

        self.workerpool = self.bldr.getAvailableWorkers()

        # Pick workers one at a time from the pool, and if the Builder says
        # they're usable (eg, locks can be satisfied), then prefer those
        # workers.
        self.preferredWorkers = []

        self.nextBuild = self.bldr.config.nextBuild

    @defer.inlineCallbacks
    def popNextBuild(self):
        nextBuild = (None, None)

        while True:
            #  1. pick a build
            breq = yield self._getNextUnclaimedBuildRequest()
            if not breq:
                break

            if not self.workerpool and not self.preferredWorkers:
                self.workerpool = self.bldr.getAvailableWorkers()

            #  2. pick a worker
            worker = yield self._popNextWorker(breq)
            if not worker:
                break

            # either satisfy this build or we leave it for another day
            self._removeBuildRequest(breq)

            #  3. make sure worker+ is usable for the breq
            recycledWorkers = []
            while worker:
                canStart = yield self.canStartBuild(worker, breq)
                if canStart:
                    break
                # try a different worker
                recycledWorkers.append(worker)
                worker = yield self._popNextWorker(breq)

            # recycle the workers that we didn't use to the head of the queue
            # this helps ensure we run 'nextWorker' only once per worker choice
            if recycledWorkers:
                self._unpopWorkers(recycledWorkers)

            #  4. done? otherwise we will try another build
            if worker:
                nextBuild = (worker, breq)
                break

        return nextBuild

    @defer.inlineCallbacks
    def _getNextUnclaimedBuildRequest(self):
        # ensure the cache is there
        yield self._fetchUnclaimedBrdicts()
        if not self.unclaimedBrdicts:
            return None

        if self.nextBuild:
            # nextBuild expects BuildRequest objects
            breqs = yield self._getUnclaimedBuildRequests()
            try:
                nextBreq = yield self.nextBuild(self.bldr, breqs)
                if nextBreq not in breqs:
                    nextBreq = None
            except Exception:
                log.err(Failure(), f"from _getNextUnclaimedBuildRequest for builder '{self.bldr}'")
                nextBreq = None
        else:
            # otherwise just return the build with highest priority
            brdict = sorted(self.unclaimedBrdicts.data, key=lambda b: b['priority'], reverse=True)[
                0
            ]
            nextBreq = yield self._getBuildRequestForBrdict(brdict)

        return nextBreq

    @defer.inlineCallbacks
    def _popNextWorker(self, buildrequest):
        # use 'preferred' workers first, if we have some ready
        if self.preferredWorkers:
            worker = self.preferredWorkers.pop(0)
            return worker

        while self.workerpool:
            try:
                worker = yield self.nextWorker(self.bldr, self.workerpool, buildrequest)
            except Exception:
                log.err(Failure(), f"from nextWorker for builder '{self.bldr}'")
                worker = None

            if not worker or worker not in self.workerpool:
                # bad worker or no worker returned
                break

            self.workerpool.remove(worker)
            return worker

        return None

    def _unpopWorkers(self, workers):
        # push the workers back to the front
        self.preferredWorkers[:0] = workers

    def canStartBuild(self, worker, breq):
        return self.bldr.canStartBuild(worker, breq)


class BuildRequestDistributor(service.AsyncMultiService):
    """
    Special-purpose class to handle distributing build requests to builders by
    calling their C{maybeStartBuild} method.

    This takes account of the C{prioritizeBuilders} configuration, and is
    highly re-entrant; that is, if a new build request arrives while builders
    are still working on the previous build request, then this class will
    correctly re-prioritize invocations of builders' C{maybeStartBuild}
    methods.
    """

    BuildChooser = BasicBuildChooser

    def __init__(self, botmaster):
        super().__init__()
        self.botmaster = botmaster

        # lock to ensure builders are only sorted once at any time
        self.pending_builders_lock = defer.DeferredLock()

        # sorted list of names of builders that need their maybeStartBuild
        # method invoked.
        self._pending_builders = []
        self.activity_lock = defer.DeferredLock()
        self.active = False

        self._deferwaiter = deferwaiter.DeferWaiter()
        self._activity_loop_deferred = None

        # Use in Master clean shutdown
        # this flag will allow the distributor to still
        # start new builds if it has a parent waiting on it
        self.distribute_only_waited_childs = False

    @property
    def can_distribute(self):
        return bool(self.running) or self.distribute_only_waited_childs

    @defer.inlineCallbacks
    def stopService(self):
        # Lots of stuff happens asynchronously here, so we need to let it all
        # quiesce.  First, let the parent stopService succeed between
        # activities; then the loop will stop calling itself, since
        # self.running is false.
        yield self.activity_lock.run(service.AsyncService.stopService, self)

        # now let any outstanding calls to maybeStartBuildsOn to finish, so
        # they don't get interrupted in mid-stride.  This tends to be
        # particularly painful because it can occur when a generator is gc'd.
        # TEST-TODO: this behavior is not asserted in any way.
        yield self._deferwaiter.wait()

    @async_to_deferred
    async def maybeStartBuildsOn(self, new_builders: list[str]) -> None:
        """
        Try to start any builds that can be started right now.  This function
        returns immediately, and promises to trigger those builders
        eventually.

        @param new_builders: names of new builders that should be given the
        opportunity to check for new requests.
        """
        if not self.can_distribute:
            return

        try:
            await self._deferwaiter.add(self._maybeStartBuildsOn(new_builders))
        except Exception as e:  # pragma: no cover
            log.err(e, f"while starting builds on {new_builders}")

    @async_to_deferred
    async def _maybeStartBuildsOn(self, new_builders: list[str]) -> None:
        new_builder_set = set(new_builders)
        existing_pending = set(self._pending_builders)

        # if we won't add any builders, there's nothing to do
        if new_builder_set < existing_pending:
            return

        # reset the list of pending builders
        try:
            async with self.pending_builders_lock:
                # re-fetch existing_pending, in case it has changed
                # while acquiring the lock
                existing_pending = set(self._pending_builders)

                # then sort the new, expanded set of builders
                self._pending_builders = await self._sortBuilders(
                    list(existing_pending | new_builder_set)
                )

                # start the activity loop, if we aren't already
                # working on that.
                if not self.active:
                    self._activity_loop_deferred = defer.ensureDeferred(self._activityLoop())
        except Exception:  # pragma: no cover
            log.err(Failure(), f"while attempting to start builds on {self.name}")

    @defer.inlineCallbacks
    def _defaultSorter(self, master, builders):
        timer = metrics.Timer("BuildRequestDistributor._defaultSorter()")
        timer.start()

        @defer.inlineCallbacks
        def key(bldr):
            # Sort primarily highest priority of build requests
            priority = yield bldr.get_highest_priority()
            if priority is None:
                # for builders that do not have pending buildrequest, we just use large number
                priority = -math.inf
            # Break ties using the time of oldest build request
            time = yield bldr.getOldestRequestTime()
            if time is None:
                # for builders that do not have pending buildrequest, we just use large number
                time = math.inf
            else:
                if isinstance(time, datetime):
                    time = time.timestamp()
            return (-priority, time, bldr.name)

        yield async_sort(builders, key)

        timer.stop()
        return builders

    @defer.inlineCallbacks
    def _sortBuilders(self, buildernames):
        timer = metrics.Timer("BuildRequestDistributor._sortBuilders()")
        timer.start()
        # note that this takes and returns a list of builder names

        # convert builder names to builders
        builders_dict = self.botmaster.builders
        builders = [builders_dict.get(n) for n in buildernames if n in builders_dict]

        # find a sorting function
        sorter = self.master.config.prioritizeBuilders
        if not sorter:
            sorter = self._defaultSorter

        # run it
        try:
            builders = yield sorter(self.master, builders)
        except Exception:
            log.err(Failure(), "prioritizing builders; order unspecified")

        # and return the names
        rv = [b.name for b in builders]
        timer.stop()
        return rv

    @metrics.timeMethod('BuildRequestDistributor._activityLoop()')
    async def _activityLoop(self) -> None:
        self.active = True

        pending_builders: list[Builder] = []
        while True:
            async with self.activity_lock:
                if not self.can_distribute:
                    break

                if not pending_builders:
                    # lock pending_builders, pop an element from it, and release
                    async with self.pending_builders_lock:
                        # bail out if we shouldn't keep looping
                        if not self._pending_builders:
                            break
                        # take that builder list, and run it until the end
                        # we make a copy of it, as it could be modified meanwhile
                        pending_builders = copy.copy(self._pending_builders)
                        self._pending_builders = []

                bldr_name = pending_builders.pop(0)

                # get the actual builder object
                bldr = self.botmaster.builders.get(bldr_name)
                try:
                    if bldr:
                        await self._maybeStartBuildsOnBuilder(bldr)
                except Exception:
                    log.err(Failure(), f"from maybeStartBuild for builder '{bldr_name}'")

        self.active = False

    async def _maybeStartBuildsOnBuilder(self, bldr: Builder) -> None:
        # create a chooser to give us our next builds
        # this object is temporary and will go away when we're done
        bc = self.createBuildChooser(bldr, self.master)

        while True:
            worker, breqs = await bc.chooseNextBuild()
            if not worker or not breqs:
                break

            if self.distribute_only_waited_childs:
                # parenting is a field of Buildset
                # get the buildsets only for requests
                # that are waited for
                buildset_ids = set(br.bsid for br in breqs if br.waited_for)
                if not buildset_ids:
                    continue
                # get buildsets if they have a parent
                buildsets_data: list[dict] = await self.master.data.get(
                    ('buildsets',),
                    filters=[
                        resultspec.Filter('bsid', 'in', buildset_ids),
                        resultspec.Filter('parent_buildid', 'ne', [None]),
                    ],
                    fields=['bsid', 'parent_buildid'],
                )
                parented_buildset_ids = set(bs['bsid'] for bs in buildsets_data)
                breqs = [br for br in breqs if br.bsid in parented_buildset_ids]
                if not breqs:
                    continue

            # claim brid's
            brids = [br.id for br in breqs]
            claimed_at_epoch = self.master.reactor.seconds()
            claimed_at = epoch2datetime(claimed_at_epoch)

            self._add_in_progress_brids(brids)
            if not (
                await self.master.data.updates.claimBuildRequests(brids, claimed_at=claimed_at)
            ):
                # some brids were already claimed, so start over
                bc = self.createBuildChooser(bldr, self.master)
                continue

            buildStarted = await bldr.maybeStartBuild(worker, breqs)
            if not buildStarted:
                await self.master.data.updates.unclaimBuildRequests(brids)
                self._remove_in_progress_brids(brids)

                # try starting builds again.  If we still have a working worker,
                # then this may re-claim the same buildrequests
                self.botmaster.maybeStartBuildsForBuilder(self.name)

    def _add_in_progress_brids(self, brids):
        for brid in brids:
            self.master.botmaster.add_in_progress_buildrequest(brid)

    def _remove_in_progress_brids(self, brids):
        for brid in brids:
            self.master.botmaster.remove_in_progress_buildrequest(brid)

    def createBuildChooser(self, bldr, master):
        # just instantiate the build chooser requested
        return self.BuildChooser(bldr, master)

    @async_to_deferred
    async def _waitForFinish(self):
        if self._activity_loop_deferred is not None:
            await self._activity_loop_deferred
