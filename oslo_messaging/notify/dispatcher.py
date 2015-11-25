# Copyright 2011 OpenStack Foundation.
# All Rights Reserved.
# Copyright 2013 eNovance
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

import itertools
import logging
import sys

import six

from oslo_messaging import _utils as utils
from oslo_messaging import localcontext
from oslo_messaging import serializer as msg_serializer


LOG = logging.getLogger(__name__)

PRIORITIES = ['audit', 'debug', 'info', 'warn', 'error', 'critical', 'sample']


class NotificationResult(object):
    HANDLED = 'handled'
    REQUEUE = 'requeue'


class _NotificationDispatcherBase(object):
    def __init__(self, targets, endpoints, serializer, allow_requeue,
                 pool=None):
        self.targets = targets
        self.endpoints = endpoints
        self.serializer = serializer or msg_serializer.NoOpSerializer()
        self.allow_requeue = allow_requeue
        self.pool = pool

        self._callbacks_by_priority = {}
        for endpoint, prio in itertools.product(endpoints, PRIORITIES):
            if hasattr(endpoint, prio):
                method = getattr(endpoint, prio)
                screen = getattr(endpoint, 'filter_rule', None)
                self._callbacks_by_priority.setdefault(prio, []).append(
                    (screen, method))

        priorities = self._callbacks_by_priority.keys()
        self._targets_priorities = set(itertools.product(self.targets,
                                                         priorities))

    def _listen(self, transport):
        return transport._listen_for_notifications(self._targets_priorities,
                                                   pool=self.pool)

    def __call__(self, incoming, executor_callback=None):
        return utils.DispatcherExecutorContext(
            incoming, self._dispatch_and_handle_error,
            executor_callback=executor_callback,
            post=self._post_dispatch)

    def _post_dispatch(self, incoming, requeues):
        for m in self._get_messages(incoming):
            try:
                if requeues and m in requeues:
                    m.requeue()
                else:
                    m.acknowledge()
            except Exception:
                # sys.exc_info() is deleted by LOG.exception().
                exc_info = sys.exc_info()
                LOG.error("Fail to ack/requeue message", exc_info=exc_info)

    def _dispatch_and_handle_error(self, incoming, executor_callback):
        """Dispatch a notification message to the appropriate endpoint method.

        :param incoming: the incoming notification message
        :type ctxt: IncomingMessage
        """
        try:
            return self._dispatch(incoming, executor_callback)
        except Exception:
            # sys.exc_info() is deleted by LOG.exception().
            exc_info = sys.exc_info()
            LOG.error('Exception during message handling',
                      exc_info=exc_info)

    def _dispatch(self, incoming, executor_callback=None):
        """Dispatch notification messages to the appropriate endpoint method.
        """

        messages_grouped = itertools.groupby((
            self._extract_user_message(m)
            for m in self._get_messages(incoming)), lambda x: x[0])

        requeues = set()
        for priority, messages in messages_grouped:
            __, raw_messages, messages = six.moves.zip(*messages)
            raw_messages = list(raw_messages)
            messages = list(messages)
            if priority not in PRIORITIES:
                LOG.warning('Unknown priority "%s"', priority)
                continue
            for screen, callback in self._callbacks_by_priority.get(priority,
                                                                    []):
                if screen:
                    filtered_messages = [message for message in messages
                                         if screen.match(
                                             message["ctxt"],
                                             message["publisher_id"],
                                             message["event_type"],
                                             message["metadata"],
                                             message["payload"])]
                else:
                    filtered_messages = messages

                if not filtered_messages:
                    continue

                ret = self._exec_callback(executor_callback, callback,
                                          filtered_messages)
                if self.allow_requeue and ret == NotificationResult.REQUEUE:
                    requeues.update(raw_messages)
                    break
        return requeues

    def _exec_callback(self, executor_callback, callback, *args):
        if executor_callback:
            ret = executor_callback(callback, *args)
        else:
            ret = callback(*args)
        return NotificationResult.HANDLED if ret is None else ret

    def _extract_user_message(self, incoming):
        ctxt = self.serializer.deserialize_context(incoming.ctxt)
        message = incoming.message

        publisher_id = message.get('publisher_id')
        event_type = message.get('event_type')
        metadata = {
            'message_id': message.get('message_id'),
            'timestamp': message.get('timestamp')
        }
        priority = message.get('priority', '').lower()
        payload = self.serializer.deserialize_entity(ctxt,
                                                     message.get('payload'))
        return priority, incoming, dict(ctxt=ctxt,
                                        publisher_id=publisher_id,
                                        event_type=event_type,
                                        payload=payload,
                                        metadata=metadata)


class NotificationDispatcher(_NotificationDispatcherBase):
    """A message dispatcher which understands Notification messages.

    A MessageHandlingServer is constructed by passing a callable dispatcher
    which is invoked with context and message dictionaries each time a message
    is received.
    """
    def _get_messages(self, incoming):
        return [incoming]

    def _exec_callback(self, executor_callback, callback, messages):
        localcontext._set_local_context(
            messages[0]["ctxt"])
        try:
            return super(NotificationDispatcher, self)._exec_callback(
                executor_callback, callback,
                messages[0]["ctxt"],
                messages[0]["publisher_id"],
                messages[0]["event_type"],
                messages[0]["payload"],
                messages[0]["metadata"])
        finally:
            localcontext._clear_local_context()


class BatchNotificationDispatcher(_NotificationDispatcherBase):
    """A message dispatcher which understands Notification messages.

    A MessageHandlingServer is constructed by passing a callable dispatcher
    which is invoked with a list of message dictionaries each time 'batch_size'
    messages are received or 'batch_timeout' seconds is reached.
    """

    def __init__(self, targets, endpoints, serializer, allow_requeue,
                 pool=None, batch_size=None, batch_timeout=None):
        super(BatchNotificationDispatcher, self).__init__(targets, endpoints,
                                                          serializer,
                                                          allow_requeue,
                                                          pool)
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.batch_mode = True

    def _get_messages(self, incoming):
        return incoming
