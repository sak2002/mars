# Copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import concurrent.futures as futures
import itertools
import logging
import time
from abc import ABC
from collections import namedtuple
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, Type
from urllib.parse import urlparse

from ....oscar.profiling import ProfilingData
from ....serialization import serialize, deserialize
from ....utils import lazy_import, implements, classproperty, Timer
from ...debug import debug_async_timeout
from ...errors import ServerClosed
from ..communication.base import Channel, ChannelType, Server, Client
from ..communication.core import register_client, register_server
from ..communication.errors import ChannelClosed

ray = lazy_import("ray")
logger = logging.getLogger(__name__)

ChannelID = namedtuple(
    "ChannelID", ["local_address", "client_id", "channel_index", "dest_address"]
)


def _argwrapper_unpickler(serialized_message):
    return _ArgWrapper(deserialize(*serialized_message))


@dataclass
class _ArgWrapper:
    message: Any = None

    def __init__(self, message):
        self.message = message

    def __reduce__(self):
        return _argwrapper_unpickler, (serialize(self.message),)


if ray:
    # Note: Must init metrics before using and here initializing metrics
    # with ray backend.
    from ....metrics import init_metrics

    init_metrics("ray")
    _ray_serialize = ray.serialization.SerializationContext.serialize
    _ray_deserialize_object = ray.serialization.SerializationContext._deserialize_object

    def _serialize(self, value):
        if type(value) is _ArgWrapper:  # pylint: disable=unidiomatic-typecheck
            message = value.message
            with Timer() as timer:
                serialized_object = _ray_serialize(self, value)
            try:
                if message.profiling_context is not None:
                    task_id = message.profiling_context.task_id
                    ProfilingData[task_id, "serialization"].inc(
                        "serialize", timer.duration
                    )
            except AttributeError:
                logger.debug(
                    "Profiling serialization got error, the send "
                    "message %s may not be an instance of message",
                    type(message),
                )
        else:
            serialized_object = _ray_serialize(self, value)
        return serialized_object

    def _deserialize_object(self, data, metadata, object_ref):
        start_time = time.time()
        value = _ray_deserialize_object(self, data, metadata, object_ref)
        if type(value) is _ArgWrapper:  # pylint: disable=unidiomatic-typecheck
            message = value.message
            try:
                if message.profiling_context is not None:
                    task_id = message.profiling_context.task_id
                    ProfilingData[task_id, "serialization"].inc(
                        "deserialize", time.time() - start_time
                    )
            except AttributeError:
                logger.debug(
                    "Profiling serialization got error, the recv "
                    "message %s may not be an instance of message",
                    type(message),
                )
        return value

    ray.serialization.SerializationContext.serialize = _serialize
    ray.serialization.SerializationContext._deserialize_object = _deserialize_object


class RayChannelException(Exception):
    def __init__(self, exc_type, exc_value: BaseException, exc_traceback):
        self.exc_type = exc_type
        self.exc_value = exc_value
        self.exc_traceback = exc_traceback


class RayChannelBase(Channel, ABC):
    """
    Channel for communications between ray processes.
    """

    __slots__ = "_channel_index", "_channel_id", "_closed"

    name = "ray"
    _channel_index_gen = itertools.count()

    def __init__(
        self,
        local_address: str = None,
        dest_address: str = None,
        channel_index: int = None,
        channel_id: ChannelID = None,
        compression=None,
    ):
        super().__init__(
            local_address=local_address,
            dest_address=dest_address,
            compression=compression,
        )
        self._channel_index = channel_index or next(self._channel_index_gen)
        self._channel_id = channel_id or ChannelID(
            local_address, _gen_client_id(), self._channel_index, dest_address
        )
        self._closed = asyncio.Event()

    @property
    def channel_id(self) -> ChannelID:
        return self._channel_id

    @property
    @implements(Channel.type)
    def type(self) -> ChannelType:
        return ChannelType.ray

    @implements(Channel.close)
    async def close(self):
        self._closed.set()

    @property
    @implements(Channel.closed)
    def closed(self) -> bool:
        return self._closed.is_set()


class RayClientChannel(RayChannelBase):
    """
    A channel from ray driver/actor to ray actor. Use ray call reply for client channel recv.
    """

    __slots__ = "_peer_actor", "_done", "_todo"

    def __init__(
        self,
        dest_address: str = None,
        channel_index: int = None,
        channel_id: ChannelID = None,
        compression=None,
    ):
        super().__init__(None, dest_address, channel_index, channel_id, compression)
        # ray actor should be created with the address as the name.
        self._peer_actor: "ray.actor.ActorHandle" = ray.get_actor(dest_address)
        self._done = asyncio.Queue()
        self._todo = set()

    def _submit_task(self, message: Any, object_ref: "ray.ObjectRef"):
        async def handle_task(message: Any, object_ref: "ray.ObjectRef"):
            # use `%.500` to avoid print too long messages
            with debug_async_timeout(
                "ray_object_retrieval_timeout", "Client sent message is %.500s", message
            ):
                result = await object_ref
            if isinstance(result, RayChannelException):
                raise result.exc_value.with_traceback(result.exc_traceback)
            return result.message

        def _on_completion(future):
            self._todo.remove(future)
            self._done.put_nowait(future)

        future = asyncio.ensure_future(handle_task(message, object_ref))
        future.add_done_callback(_on_completion)
        self._todo.add(future)

    @implements(Channel.send)
    async def send(self, message: Any):
        if self._closed.is_set():  # pragma: no cover
            raise ChannelClosed("Channel already closed, cannot send message")
        # Put ray object ref to todo queue
        task = self._peer_actor.__on_ray_recv__.remote(
            self.channel_id, _ArgWrapper(message)
        )
        self._submit_task(message, task)
        await asyncio.sleep(0)

    @implements(Channel.recv)
    async def recv(self):
        if self._closed.is_set():  # pragma: no cover
            raise ChannelClosed("Channel already closed, cannot recv message")
        try:
            # Wait first done.
            future = await self._done.get()
            return future.result()
        except ray.exceptions.RayActorError:
            if not self._closed.is_set():
                # raise a EOFError as the SocketChannel does
                raise EOFError("Server may be closed")
        except (RuntimeError, ServerClosed) as e:  # pragma: no cover
            if not self._closed.is_set():
                raise e


class RayServerChannel(RayChannelBase):
    """
    A channel from ray actor to ray driver/actor. Since ray actor can't call ray driver,
    we use ray call reply for server channel send. Note that there can't be multiple
    channel message sends for one received message, or else it will be taken as next
    message's reply.
    """

    __slots__ = "_in_queue", "_out_queue", "_msg_recv_counter", "_msg_sent_counter"

    def __init__(
        self,
        local_address: str = None,
        channel_index: int = None,
        channel_id: ChannelID = None,
        compression=None,
    ):
        super().__init__(local_address, None, channel_index, channel_id, compression)
        self._in_queue = asyncio.Queue()
        self._out_queue = asyncio.Queue()
        self._msg_recv_counter = 0
        self._msg_sent_counter = 0

    @implements(Channel.send)
    async def send(self, message: Any):
        if self._closed.is_set():  # pragma: no cover
            raise ChannelClosed("Channel already closed, cannot send message")
        # Current process is ray actor, we use ray call reply to send message to ray driver/actor.
        # Not that we can only send once for every read message in channel, otherwise
        # it will be taken as other message's reply.
        await self._out_queue.put(message)
        self._msg_sent_counter += 1
        assert (
            self._msg_sent_counter <= self._msg_recv_counter
        ), "RayServerChannel channel doesn't support send multiple replies for one message."

    @implements(Channel.recv)
    async def recv(self):
        if self._closed.is_set():  # pragma: no cover
            raise ChannelClosed("Channel already closed, cannot write message")
        try:
            return await self._in_queue.get()
        except RuntimeError:  # pragma: no cover
            if not self._closed.is_set():
                raise

    async def __on_ray_recv__(self, message_wrapper):
        """This method will be invoked when current process is a ray actor rather than a ray driver"""
        self._msg_recv_counter += 1
        await self._in_queue.put(message_wrapper.message)
        result_message = await self._out_queue.get()
        if self._closed.is_set():  # pragma: no cover
            raise ChannelClosed("Channel already closed")
        return _ArgWrapper(result_message)

    @implements(Channel.close)
    async def close(self):
        await super().close()
        self._out_queue.put_nowait(None)


@register_server
class RayServer(Server):
    __slots__ = "_closed", "_channels", "_tasks"

    scheme = "ray"
    _server_instance = None
    _ray_actor_started = False

    def __init__(self, address, channel_handler: Callable[[Channel], Coroutine] = None):
        super().__init__(address, channel_handler)
        self._closed = asyncio.Event()
        self._channels: Dict[ChannelID, RayServerChannel] = dict()
        self._tasks: Dict[ChannelID, asyncio.Task] = dict()

    @classproperty
    @implements(Server.client_type)
    def client_type(self) -> Type["Client"]:
        return RayClient

    @property
    @implements(Server.channel_type)
    def channel_type(self) -> ChannelType:
        return ChannelType.ray

    @classmethod
    def set_ray_actor_started(cls):
        cls._ray_actor_started = True

    @classmethod
    def is_ray_actor_started(cls):
        return cls._ray_actor_started

    @staticmethod
    @implements(Server.create)
    async def create(config: Dict) -> "RayServer":
        if not RayServer.is_ray_actor_started():
            logger.warning(
                "Current process is not a ray actor, the ray server "
                "will not receive messages from clients."
            )
        assert RayServer._server_instance is None
        config = config.copy()
        address = config.pop("address")
        handle_channel = config.pop("handle_channel")
        if urlparse(address).scheme != RayServer.scheme:  # pragma: no cover
            raise ValueError(
                f"Address for RayServer "
                f'should be starts with "ray://", '
                f"got {address}"
            )
        if config:  # pragma: no cover
            raise TypeError(
                f"Creating RayServer got unexpected " f'arguments: {",".join(config)}'
            )
        server = RayServer(address, handle_channel)
        RayServer._server_instance = server
        return server

    @classmethod
    def get_instance(cls):
        return cls._server_instance

    @classmethod
    def clear(cls):
        cls._server_instance = None
        cls._ray_actor_started = False

    @implements(Server.start)
    async def start(self):
        # nothing needs to do for ray server
        pass

    @implements(Server.join)
    async def join(self, timeout=None):
        wait_coro = self._closed.wait()
        try:
            await asyncio.wait_for(wait_coro, timeout=timeout)
        except (futures.TimeoutError, asyncio.TimeoutError):  # pragma: no cover
            pass

    @implements(Server.on_connected)
    async def on_connected(self, *args, **kwargs):
        channel = args[0]
        assert isinstance(channel, RayServerChannel)
        if kwargs:  # pragma: no cover
            raise TypeError(
                f"{type(self).__name__} got unexpected "
                f'arguments: {",".join(kwargs)}'
            )
        await self.channel_handler(channel)

    @implements(Server.stop)
    async def stop(self):
        self._closed.set()
        for task in self._tasks.values():
            task.cancel()
        self._tasks = dict()
        for channel in self._channels.values():
            await channel.close()
        self._channels = dict()
        self.clear()

    @property
    @implements(Server.stopped)
    def stopped(self) -> bool:
        return self._closed.is_set()

    async def __on_ray_recv__(self, channel_id: ChannelID, message):
        if self.stopped:
            raise ServerClosed(
                f"Remote server {self.address} closed, but got message {message} "
                f"from channel {channel_id}"
            )
        channel = self._channels.get(channel_id)
        if not channel:
            _, _, peer_channel_index, peer_dest_address = channel_id
            channel = RayServerChannel(
                peer_dest_address, peer_channel_index, channel_id
            )
            self._channels[channel_id] = channel
            self._tasks[channel_id] = asyncio.create_task(self.on_connected(channel))
        return await channel.__on_ray_recv__(message)


@register_client
class RayClient(Client):
    __slots__ = ()

    scheme = RayServer.scheme

    def __init__(self, local_address: str, dest_address: str, channel: Channel):
        super().__init__(local_address, dest_address, channel)

    @staticmethod
    @implements(Client.connect)
    async def connect(
        dest_address: str, local_address: str = None, **kwargs
    ) -> "Client":
        if urlparse(dest_address).scheme != RayServer.scheme:  # pragma: no cover
            raise ValueError(
                f'Destination address should start with "ray://" '
                f"for RayClient, got {dest_address}"
            )
        client_channel = RayClientChannel(dest_address)
        client = RayClient(local_address, dest_address, client_channel)
        return client

    @implements(Client.close)
    async def close(self):
        await super().close()


def _gen_client_id():
    import uuid

    return uuid.uuid4().hex
