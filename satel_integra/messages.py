"""Message classes for communication with Satel Integra panel."""

import logging
from typing import TypeVar

from satel_integra.commands import SatelBaseCommand, SatelReadCommand, SatelWriteCommand
from satel_integra.const import (
    FRAME_END,
    FRAME_SPECIAL_BYTES,
    FRAME_SPECIAL_BYTES_REPLACEMENT,
    FRAME_START,
)
from satel_integra.utils import checksum, decode_bitmask_le, encode_bitmask_le

_LOGGER = logging.getLogger(__name__)

# LOG DI TEST: questo deve apparire all'import del modulo
_LOGGER.info("messages.py LOCALE CARICATO - versione modificata con try/except per comandi custom")


TCommand = TypeVar("TCommand", bound=SatelBaseCommand)


class SatelBaseMessage[TCommand: SatelBaseCommand]:
    """Base class shared by read/write message types."""

    def __init__(self, cmd: TCommand | int, msg_data: bytearray) -> None:
        self.cmd = cmd
        self.msg_data = msg_data

    def __str__(self) -> str:
        """Format message string as (SatelMessage) CMD [CMD_HEX] -> DATA_HEX (DATA_LENGTH)"""
        cmd_str = f"0x{self.cmd:02X}" if isinstance(self.cmd, int) else str(self.cmd)
        return f"({self.__class__.__name__}) {cmd_str} -> {self.msg_data.hex()} ({len(self.msg_data)})"


class SatelWriteMessage(SatelBaseMessage[SatelWriteCommand]):
    """Message used to send commands to the panel."""

    def __init__(
        self,
        cmd: SatelWriteCommand | int,
        code: str | None = None,
        partitions: list[int] | None = None,
        zones_or_outputs: list[int] | None = None,
        raw_data: bytearray | None = None,
    ) -> None:
        msg_data = bytearray()

        if raw_data is not None:
            msg_data += raw_data
        else:
            if code:
                msg_data += bytearray.fromhex(code.strip().ljust(16, "F"))
            if partitions:
                msg_data += encode_bitmask_le(partitions, 4)
            if zones_or_outputs:
                msg_data += encode_bitmask_le(zones_or_outputs, 32)

        super().__init__(cmd, msg_data)

    def encode_frame(self) -> bytearray:
        """Construct full message frame for sending to panel."""
        data = self.cmd.to_bytearray() + self.msg_data
        csum = checksum(data)
        data.append(csum >> 8)
        data.append(csum & 0xFF)
        data = data.replace(FRAME_SPECIAL_BYTES, FRAME_SPECIAL_BYTES_REPLACEMENT)
        return bytearray(FRAME_START) + data + bytearray(FRAME_END)


class SatelReadMessage(SatelBaseMessage[SatelReadCommand | int]):
    """Message representing data received from the panel."""

    @staticmethod
    def decode_frame(
        data: bytes,
    ) -> "SatelReadMessage":
        """Verify checksum and strip header/footer of received frame."""
        _LOGGER.debug("decode_frame chiamata con data len=%d, primi 10 byte: %s", len(data), data[:10].hex())

        if data[0:2] != FRAME_START:
            _LOGGER.error("Bad header: %s", data.hex())
            raise ValueError("Invalid frame header")
        if data[-2:] != FRAME_END:
            _LOGGER.error("Bad footer: %s", data.hex())
            raise ValueError("Invalid frame footer")

        output = data[2:-2].replace(
            FRAME_SPECIAL_BYTES_REPLACEMENT, FRAME_SPECIAL_BYTES
        )
        calc_sum = checksum(output[:-2])
        received_sum = (output[-2] << 8) | output[-1]

        if received_sum != calc_sum:
            msg = f"Checksum mismatch: got {received_sum}, expected {calc_sum}"
            _LOGGER.error(msg)
            raise ValueError(msg)

        cmd_byte, msg_data = output[0], output[1:-2]

        _LOGGER.debug("cmd_byte ricevuto: 0x%02X", cmd_byte)

        cmd = None
        try:
            cmd = SatelReadCommand(cmd_byte)
            _LOGGER.info("Comando riconosciuto: %s (0x%02X)", cmd, cmd_byte)
        except ValueError as ve:
            _LOGGER.info("Comando sconosciuto ricevuto: 0x%02X - ignorato per compatibilita' (payload raw: %s)", cmd_byte, msg_data.hex())
            cmd = cmd_byte  # Usa il byte come intero

        msg = SatelReadMessage(cmd, bytearray(msg_data))
        _LOGGER.debug("Messaggio decodificato: %s", msg)
        return msg

    def get_active_bits(self, expected_length: int) -> list[int]:
        """Convenience wrapper around decode_bitmask_le() for this message."""
        return decode_bitmask_le(self.msg_data, expected_length)
