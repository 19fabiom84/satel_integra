"""Queue class for Satel Integra"""

import asyncio
from collections.abc import Callable, Awaitable

import logging

from satel_integra.commands import SatelReadCommand, SatelWriteCommand
from satel_integra.const import MESSAGE_RESPONSE_TIMEOUT
from satel_integra.messages import SatelReadMessage, SatelWriteMessage

_LOGGER = logging.getLogger(__name__)


class QueuedMessage:
    def __init__(self, message: SatelWriteMessage, wait_for_result: bool):
        self.message = message
        self.return_result = wait_for_result

        self.processed_future: asyncio.Future[SatelReadMessage] = (
            asyncio.get_running_loop().create_future()
        )

        # Comandi che restituiscono echo del comando (non RESULT)
        echo_commands = {
            SatelWriteCommand.READ_EVENT,
            SatelWriteCommand.READ_EVENT_TEXT,
        }

        if message.cmd in echo_commands:
            self.expected_result_command = message.cmd  # echo stesso comando
        else:
            self.expected_result_command = SatelReadCommand.RESULT


class SatelMessageQueue:
    """Queue ensuring write commands are sent sequentially and wait for a result."""

    def __init__(self, send_func: Callable[[SatelWriteMessage], Awaitable[None]]):
        self._send_func = send_func
        self._queue: asyncio.Queue[QueuedMessage] = asyncio.Queue()

        self._current_message: QueuedMessage | None = None
        self._process_task: asyncio.Task | None = None
        self._closed = False

    async def start(self):
        if self._process_task:
            return
        self._process_task = asyncio.create_task(self._process_queue())

    async def stop(self):
        self._closed = True
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
            self._process_task = None

    async def add_message(self, msg: SatelWriteMessage, wait_for_result: bool = False):
        if self._closed:
            raise RuntimeError("Queue is stopped")

        _LOGGER.debug("Queueing message: %s", msg)

        queued = QueuedMessage(msg, wait_for_result)
        await self._queue.put(queued)

        if not wait_for_result:
            return

        try:
            return await queued.processed_future
        except Exception as exc:
            _LOGGER.debug("Couldn't wait for message result: %s", exc)
            return

    async def _process_queue(self) -> None:
        _LOGGER.debug("Message queue worker started")

        while not self._closed:
            try:
                self._current_message = await self._get_next_message()
                if self._current_message is None:
                    continue

                await self._send_and_wait_response(self._current_message)

            except Exception as e:
                _LOGGER.exception("Unexpected error in queue processing: %s", e)

            finally:
                self._current_message = None

        _LOGGER.debug("Command queue worker stopped")

    async def _get_next_message(self) -> QueuedMessage | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

    async def _send_and_wait_response(self, queued: QueuedMessage) -> None:
        try:
            _LOGGER.debug("Sending message: %s", queued.message)
            await self._send_func(queued.message)
        except Exception as exc:
            _LOGGER.debug("Error while sending message: %s", exc)
            if not queued.processed_future.done():
                queued.processed_future.set_exception(exc)
            return

        # Wait for the expected response
        try:
            await asyncio.wait_for(
                queued.processed_future, timeout=MESSAGE_RESPONSE_TIMEOUT
            )
        except asyncio.TimeoutError:
            _LOGGER.error(
                "No response received from panel within %ss for cmd=0x%02X",
                MESSAGE_RESPONSE_TIMEOUT, queued.message.cmd.value
            )
            return

    def on_message_received(self, result: SatelReadMessage):
        """Called by AsyncSatel when a message is received."""
        if not self._current_message:
            return  # messaggio asincrono da monitoring

        if self._current_message.processed_future.done():
            _LOGGER.debug("Received result but future already done")
            return

        expected = self._current_message.expected_result_command
        sent_cmd = self._current_message.message.cmd

        # Accetta sia RESULT che echo del comando inviato
        if result.cmd == expected or result.cmd == sent_cmd:
            _LOGGER.info("Risposta attesa ricevuta: %s (attesa: %s o echo %s)", result.cmd, expected, sent_cmd)
            self._current_message.processed_future.set_result(result)
        else:
            _LOGGER.warning(
                "Risposta inattesa: %s (attesa: %s o echo %s)",
                result.cmd, expected, sent_cmd
            )
