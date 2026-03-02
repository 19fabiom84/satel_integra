"""Main module."""

import asyncio
import logging
from enum import Enum, unique
from collections.abc import Callable

from satel_integra.commands import SatelReadCommand, SatelWriteCommand
from satel_integra.connection import SatelConnection
from satel_integra.messages import SatelReadMessage, SatelWriteMessage
from satel_integra.utils import encode_bitmask_le
from satel_integra.queue import SatelMessageQueue

_LOGGER = logging.getLogger(__name__)


@unique
class AlarmState(Enum):
    ARMED_MODE0 = 0
    ARMED_MODE1 = 1
    ARMED_MODE2 = 2
    ARMED_MODE3 = 3
    ARMED_SUPPRESSED = 4
    ENTRY_TIME = 5
    EXIT_COUNTDOWN_OVER_10 = 6
    EXIT_COUNTDOWN_UNDER_10 = 7
    TRIGGERED = 8
    TRIGGERED_FIRE = 9
    DISARMED = 10


class AsyncSatel:
    """Asynchronous interface to talk to Satel Integra alarm system."""

    def __init__(
        self,
        host: str,
        port: int,
        monitored_zones: list[int] = [],
        monitored_outputs: list[int] = [],
        partitions: list[int] = [],
        integration_key: str | None = None,
    ):
        self._connection = SatelConnection(host, port, integration_key=integration_key)
        self._queue = SatelMessageQueue(self._send_encoded_frame)
        self._reading_task: asyncio.Task | None = None
        self._reconnection_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_timeout = 20

        self._monitored_zones: list[int] = monitored_zones
        self.violated_zones: list[int] = []

        self._monitored_outputs: list[int] = monitored_outputs
        self.violated_outputs: list[int] = []

        self.partition_states: dict[AlarmState, list[int]] = {}
        self._partitions: list[int] = partitions

        self._alarm_status_callback: Callable[[], None] | None = None
        self._zone_changed_callback: Callable[[dict[int, int]], None] | None = None
        self._output_changed_callback: Callable[[dict[int, int]], None] | None = None

        self._message_handlers: dict[
            SatelReadCommand, Callable[[SatelReadMessage], None]
        ] = {
            SatelReadCommand.ZONES_VIOLATED: self._zones_violated,
            SatelReadCommand.PARTITIONS_ARMED_SUPPRESSED: lambda msg: (
                self._partitions_armed_state(AlarmState.ARMED_SUPPRESSED, msg)
            ),
            SatelReadCommand.PARTITIONS_ARMED_MODE0: lambda msg: (
                self._partitions_armed_state(AlarmState.ARMED_MODE0, msg)
            ),
            SatelReadCommand.PARTITIONS_ARMED_MODE2: lambda msg: (
                self._partitions_armed_state(AlarmState.ARMED_MODE2, msg)
            ),
            SatelReadCommand.PARTITIONS_ARMED_MODE3: lambda msg: (
                self._partitions_armed_state(AlarmState.ARMED_MODE3, msg)
            ),
            SatelReadCommand.PARTITIONS_ENTRY_TIME: lambda msg: (
                self._partitions_armed_state(AlarmState.ENTRY_TIME, msg)
            ),
            SatelReadCommand.PARTITIONS_EXIT_COUNTDOWN_OVER_10: lambda msg: (
                self._partitions_armed_state(AlarmState.EXIT_COUNTDOWN_OVER_10, msg)
            ),
            SatelReadCommand.PARTITIONS_EXIT_COUNTDOWN_UNDER_10: lambda msg: (
                self._partitions_armed_state(AlarmState.EXIT_COUNTDOWN_UNDER_10, msg)
            ),
            SatelReadCommand.PARTITIONS_ALARM: lambda msg: self._partitions_armed_state(
                AlarmState.TRIGGERED, msg
            ),
            SatelReadCommand.PARTITIONS_FIRE_ALARM: lambda msg: (
                self._partitions_armed_state(AlarmState.TRIGGERED_FIRE, msg)
            ),
            SatelReadCommand.OUTPUTS_STATE: self._outputs_changed,
            SatelReadCommand.PARTITIONS_ARMED_MODE1: lambda msg: (
                self._partitions_armed_state(AlarmState.ARMED_MODE1, msg)
            ),
            SatelReadCommand.RESULT: self._command_result,
        }

    # ====================== METODI NATIVI PER LETTURA EVENTI ======================

    async def read_event(self, index: bytes = b'\xFF\xFF\xFF') -> bytes | None:
        msg = SatelWriteMessage(SatelWriteCommand.READ_EVENT, raw_data=index)
        response = await self._send_data_and_wait(msg)
        if response is None:
            return None
        return response.msg_data

    async def read_event_text(self, event_record: bytes) -> str:
        if len(event_record) < 8:
            return "err record corto"

        r = (event_record[4] & 0x04) >> 2
        cc = event_record[4] & 0x03
        c8 = event_record[5]
        param = 0x8000 | (r << 10) | (cc << 8) | c8

        payload = bytes([param >> 8, param & 0xFF])
        msg = SatelWriteMessage(SatelWriteCommand.READ_EVENT_TEXT, raw_data=payload)
        response = await self._send_data_and_wait(msg)

        if response is None or len(response.msg_data) < 50:
            return "err risposta 0x8F"

        text_bytes = response.msg_data[5:]
        text = "".join(chr(b) for b in text_bytes if 32 <= b <= 126 and b != 0)
        return text.strip() or "no description"

    # ====================== METODI ORIGINALI ======================

    async def start_monitoring(self):
        monitored_commands = [
            SatelReadCommand.ZONES_VIOLATED, SatelReadCommand.PARTITIONS_ARMED_MODE0,
            SatelReadCommand.PARTITIONS_ARMED_MODE1, SatelReadCommand.PARTITIONS_ARMED_MODE2,
            SatelReadCommand.PARTITIONS_ARMED_MODE3, SatelReadCommand.PARTITIONS_ARMED_SUPPRESSED,
            SatelReadCommand.PARTITIONS_ENTRY_TIME,
            SatelReadCommand.PARTITIONS_EXIT_COUNTDOWN_OVER_10,
            SatelReadCommand.PARTITIONS_EXIT_COUNTDOWN_UNDER_10,
            SatelReadCommand.PARTITIONS_ALARM, SatelReadCommand.PARTITIONS_FIRE_ALARM,
            SatelReadCommand.OUTPUTS_STATE,
        ]

        bitmask = encode_bitmask_le([cmd.value + 1 for cmd in monitored_commands], 12)
        msg = SatelWriteMessage(SatelWriteCommand.START_MONITORING, raw_data=bytearray(bitmask))
        result = await self._send_data_and_wait(msg)

        if result is None or result.msg_data != b"\xff":
            return

    def _zones_violated(self, msg: SatelReadMessage):
        violated = msg.get_active_bits(32)
        self.violated_zones = violated
        status = {z: 1 if z in violated else 0 for z in self._monitored_zones}
        if self._zone_changed_callback:
            self._zone_changed_callback(status)

    def _outputs_changed(self, msg: SatelReadMessage):
        states = msg.get_active_bits(32)
        self.violated_outputs = states
        status = {o: 1 if o in states else 0 for o in self._monitored_outputs}
        if self._output_changed_callback:
            self._output_changed_callback(status)

    def _command_result(self, msg: SatelReadMessage):
        pass  # non serve più log

    def _partitions_armed_state(self, mode: AlarmState, msg: SatelReadMessage):
        self.partition_states[mode] = msg.get_active_bits(4)
        if self._alarm_status_callback:
            self._alarm_status_callback()

    async def start(self, enable_monitoring=True):
        await self._connection.ensure_connected()

        if not self._reading_task or self._reading_task.done():
            self._reading_task = asyncio.create_task(self._reading_loop())
        await self._queue.start()

        if not self._keepalive_task or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        if enable_monitoring:
            if not self._reconnection_task or self._reconnection_task.done():
                self._reconnection_task = asyncio.create_task(self._monitor_reconnection_loop())
            await self.start_monitoring()

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(self._keepalive_timeout)
            if self.closed:
                return
            data = SatelWriteMessage(SatelWriteCommand.READ_DEVICE_NAME, raw_data=bytearray([0x01, 0x01]))
            await self._send_data(data)

    async def _reading_loop(self):
        try:
            while not self.closed:
                await self._connection.ensure_connected()
                msg = await self._read_data()
                if not msg:
                    continue

                # === QUESTO È IL PEZZO CRITICO CHE NON DEVE MAI ESSERE RIMOSSO ===
                if (self._queue._current_message is not None or
                        msg.cmd == SatelReadCommand.RESULT or
                        getattr(msg.cmd, "expects_same_cmd_response", False)):
                    self._queue.on_message_received(msg)

                if msg.cmd in self._message_handlers:
                    self._message_handlers[msg.cmd](msg)

        except asyncio.CancelledError:
            pass
        except Exception as ex:
            _LOGGER.exception("Errore in _reading_loop: %s", ex)

    async def _monitor_reconnection_loop(self):
        while not self.closed:
            try:
                await self._connection.wait_reconnected()
                await self.start_monitoring()
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)

    def register_callbacks(self, alarm_status_callback=None, zone_changed_callback=None, output_changed_callback=None):
        if alarm_status_callback: self._alarm_status_callback = alarm_status_callback
        if zone_changed_callback: self._zone_changed_callback = zone_changed_callback
        if output_changed_callback: self._output_changed_callback = output_changed_callback

    async def arm(self, code, partition_list, mode=0):
        cmd = SatelWriteCommand(SatelWriteCommand.PARTITIONS_ARM_MODE_0 + mode)
        await self._send_data(SatelWriteMessage(cmd, code=code, partitions=partition_list))

    async def disarm(self, code, partition_list):
        await self._send_data(SatelWriteMessage(SatelWriteCommand.PARTITIONS_DISARM, code=code, partitions=partition_list))

    async def clear_alarm(self, code, partition_list):
        await self._send_data(SatelWriteMessage(SatelWriteCommand.PARTITIONS_CLEAR_ALARM, code=code, partitions=partition_list))

    async def set_output(self, code, output_id, state):
        cmd = SatelWriteCommand.OUTPUTS_ON if state else SatelWriteCommand.OUTPUTS_OFF
        await self._send_data(SatelWriteMessage(cmd, code=code, zones_or_outputs=[output_id]))

    async def _send_data(self, msg: SatelWriteMessage) -> None:
        await self._queue.add_message(msg, False)

    async def _send_data_and_wait(self, msg: SatelWriteMessage):
        return await self._queue.add_message(msg, True)

    async def _send_encoded_frame(self, msg: SatelWriteMessage) -> None:
        await self._connection.send_frame(msg.encode_frame())   # ← niente log

    async def _read_data(self) -> SatelReadMessage | None:
        try:
            data = await self._connection.read_frame()
            if not data:
                return None
            return SatelReadMessage.decode_frame(data)
        except Exception as e:
            _LOGGER.exception("Errore lettura frame: %s", e)
            return None
        finally:
            if self._alarm_status_callback:
                self._alarm_status_callback()

    @property
    def connected(self) -> bool:
        return self._connection.connected

    @property
    def closed(self) -> bool:
        return self._connection.closed

    async def connect(self) -> bool:
        return await self._connection.connect()

    async def close(self):
        await self._queue.stop()
        for task in (self._reading_task, self._reconnection_task, self._keepalive_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._connection.close()
