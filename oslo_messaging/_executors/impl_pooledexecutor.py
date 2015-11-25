# -*- coding: utf-8 -*-

#    Copyright (C) 2014 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import threading

from futurist import waiters
from oslo_config import cfg
from oslo_utils import excutils
from oslo_utils import timeutils

from oslo_messaging._executors import base

_pool_opts = [
    cfg.IntOpt('executor_thread_pool_size',
               default=64,
               deprecated_name="rpc_thread_pool_size",
               help='Size of executor thread pool.'),
]


class PooledExecutor(base.ExecutorBase):
    """A message executor which integrates with some async executor.

    This will create a message thread that polls for messages from a
    dispatching thread and on reception of an incoming message places the
    message to be processed into a async executor to be executed at a later
    time.
    """

    # These may be overridden by subclasses (and implemented using whatever
    # objects make most sense for the provided async execution model).
    _event_cls = threading.Event
    _lock_cls = threading.Lock

    # Pooling and dispatching (executor submission) will happen from a
    # thread created from this class/function.
    _thread_cls = threading.Thread

    # This one **must** be overridden by a subclass.
    _executor_cls = None

    # Blocking function that should wait for all provided futures to finish.
    _wait_for_all = staticmethod(waiters.wait_for_all)

    def __init__(self, conf, listener, dispatcher):
        super(PooledExecutor, self).__init__(conf, listener, dispatcher)
        self.conf.register_opts(_pool_opts)
        self._poller = None
        self._executor = None
        self._tombstone = self._event_cls()
        self._incomplete = collections.deque()
        self._mutator = self._lock_cls()

    def _do_submit(self, callback):
        def _on_done(fut):
            with self._mutator:
                try:
                    self._incomplete.remove(fut)
                except ValueError:
                    pass
            callback.done()
        try:
            fut = self._executor.submit(callback.run)
        except RuntimeError:
            # This is triggered when the executor has been shutdown...
            #
            # TODO(harlowja): should we put whatever we pulled off back
            # since when this is thrown it means the executor has been
            # shutdown already??
            callback.done()
            return False
        else:
            with self._mutator:
                self._incomplete.append(fut)
            # Run the other post processing of the callback when done...
            fut.add_done_callback(_on_done)
            return True

    @excutils.forever_retry_uncaught_exceptions
    def _runner(self):
        batch_mode = getattr(self.dispatcher, 'batch_mode', False)
        batch_size = getattr(self.dispatcher, 'batch_size', 1) or 1
        batch_timeout = getattr(self.dispatcher, 'batch_timeout', None)
        while not self._tombstone.is_set():
            if batch_mode:
                incoming = self.listener.batch_poll(batch_size, batch_timeout)
            else:
                incoming = self.listener.poll()
            if not incoming:
                continue
            callback = self.dispatcher(incoming, self._executor_callback)
            was_submitted = self._do_submit(callback)
            if not was_submitted:
                break

    def start(self):
        if self._executor is None:
            self._executor = self._executor_cls(
                self.conf.executor_thread_pool_size)
        self._tombstone.clear()
        if self._poller is None or not self._poller.is_alive():
            self._poller = self._thread_cls(target=self._runner)
            self._poller.daemon = True
            self._poller.start()

    def stop(self):
        if self._executor is not None:
            self._executor.shutdown(wait=False)
        self._tombstone.set()
        self.listener.stop()

    def wait(self, timeout=None):
        with timeutils.StopWatch(duration=timeout) as w:
            poller = self._poller
            if poller is not None:
                self._tombstone.wait(w.leftover(return_none=True))
                if not self._tombstone.is_set():
                    return False
                poller.join(w.leftover(return_none=True))
                if poller.is_alive():
                    return False
                self._poller = None
            executor = self._executor
            if executor is not None:
                with self._mutator:
                    incomplete_fs = list(self._incomplete)
                if incomplete_fs:
                    (done, not_done) = self._wait_for_all(
                        incomplete_fs,
                        timeout=w.leftover(return_none=True))
                    with self._mutator:
                        for fut in done:
                            try:
                                self._incomplete.remove(fut)
                            except ValueError:
                                pass
                    if not_done:
                        return False
                self._executor = None
            return True
