"""
canopen_client.py

Minimal CANopen SDO client used to configure the Baumer inclinometer
(HeartBeat producer time, filter/damping value, save & restore).

Protocol reference (from the vendor configuration notes):

    HeartBeat producer time -> object 0x1017 : 00   (UNSIGNED16, ms)
    Filter / damping value  -> object 0x2603 : 00   (UNSIGNED16)
    Store parameters        -> object 0x1010 : 01   write ASCII "save"
    Restore defaults        -> object 0x1011 : 01   write ASCII "load"

SDO client -> server (request), COB-ID = 0x600 + NodeID
SDO server -> client (response), COB-ID = 0x580 + NodeID
Heartbeat,  server -> client (cyclic), COB-ID = 0x700 + NodeID

SDO command specifiers used here (expedited transfers only):
    0x40 -> initiate upload   (read) request
    0x4B -> initiate upload response, 2 data bytes valid   (also seen as 0x43 for 4 bytes)
    0x2B -> initiate download (write) request, 2 data bytes valid
    0x23 -> initiate download (write) request, 4 data bytes valid
    0x60 -> initiate download response (write acknowledged)
    0x80 -> SDO abort (error) -- last 4 bytes = abort code
"""

from __future__ import annotations

import struct
import threading
import time
import queue
import logging
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import can  # python-can
except ImportError:  # pragma: no cover - allows the GUI to still import for editing/tests
    can = None


SDO_ABORT = 0x80
SDO_READ_REQUEST = 0x40
SDO_WRITE_1BYTE = 0x2F
SDO_WRITE_2BYTE = 0x2B
SDO_WRITE_4BYTE = 0x23
SDO_WRITE_ACK = 0x60
SDO_READ_ACK_1BYTE = 0x4F
SDO_READ_ACK_2BYTE = 0x4B
SDO_READ_ACK_4BYTE = 0x43

# Common SDO abort codes -> human readable (French, to match the rest of the app)
ABORT_CODES = {
    0x05030000: "Bascule toggle bit invalide (SDO)",
    0x05040000: "Délai d'attente dépassé (SDO timeout)",
    0x05040001: "Commande SDO invalide ou inconnue",
    0x06010000: "Accès non supporté à l'objet",
    0x06010001: "Tentative de lecture d'un objet en écriture seule",
    0x06010002: "Tentative d'écriture d'un objet en lecture seule",
    0x06020000: "Objet inexistant dans le dictionnaire",
    0x06040043: "Incompatibilité générale des paramètres",
    0x08000000: "Erreur générale",
    0x08000020: "Donnée impossible à transférer ou à stocker",
    0x08000022: "Donnée impossible en raison de l'état de l'appareil",
}


class SdoError(RuntimeError):
    """Raised when the device returns an SDO abort frame."""

    def __init__(self, index: int, subindex: int, abort_code: int):
        message = ABORT_CODES.get(abort_code, f"Code abort inconnu 0x{abort_code:08X}")
        super().__init__(
            f"SDO abort sur 0x{index:04X}:{subindex:02X} -> {message} (0x{abort_code:08X})"
        )
        self.index = index
        self.subindex = subindex
        self.abort_code = abort_code


@dataclass
class CanopenConfig:
    """Connection parameters for the PCAN adapter and the target node."""

    node_id: int = 1
    channel: str = "PCAN_USBBUS1"
    bitrate: int = 250_000
    sdo_timeout: float = 1.0  # seconds to wait for an SDO response


class CanopenClient:
    """
    Thin wrapper around python-can (PCAN backend) implementing the handful
    of expedited SDO transfers needed to configure the sensor, plus a
    heartbeat listener.

    If python-can / the PCAN driver is not available, `connect()` raises.
    """

    def __init__(self, config: CanopenConfig):
        self.config = config
        self.bus: Optional["can.BusABC"] = None
        self._lock = threading.Lock()
        self._heartbeat_listener_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self.last_heartbeat_period_ms: Optional[float] = None
        self.on_heartbeat: Optional[Callable[[float], None]] = None
        # logger for CAN traces
        self._logger = logging.getLogger("canopen_client")
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)
        # backlog for messages consumed by the heartbeat thread that are
        # not heartbeat frames (so SDO replies are not lost)
        import queue as _q

        self._rx_backlog: "_q.Queue[object]" = _q.Queue()

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        if can is None:
            raise RuntimeError(
                "python-can n'est pas installé. Faites : pip install python-can"
            )
        self.bus = can.interface.Bus(
            bustype="pcan",
            channel=self.config.channel,
            bitrate=self.config.bitrate,
        )
        self._start_heartbeat_listener()

    def start_remote_node(self) -> None:
        """Send a CANopen NMT start command for this node."""
        if self.bus is None:
            raise RuntimeError("Non connecté au bus CAN")
        msg = can.Message(
            arbitration_id=0x000,
            data=bytes([0x01, self.config.node_id]),
            is_extended_id=False,
        )
        self.bus.send(msg)
        time.sleep(0.2)

    def disconnect(self) -> None:
        self._stop_heartbeat_listener()
        if self.bus is not None:
            try:
                self.bus.shutdown()
            finally:
                self.bus = None

    @property
    def is_connected(self) -> bool:
        return self.bus is not None

    # ------------------------------------------------------------------ #
    # Low level SDO expedited transfer
    # ------------------------------------------------------------------ #
    def _cob_id_tx(self) -> int:
        return 0x600 + self.config.node_id

    def _cob_id_rx(self) -> int:
        return 0x580 + self.config.node_id

    def _send_and_wait(self, data: bytes, retries: int = 1) -> "can.Message":
        if self.bus is None:
            raise RuntimeError("Non connecté au bus CAN")

        for attempt in range(retries + 1):
            with self._lock:
                # Flush any stale frames waiting in the receive buffer.
                while True:
                    stale = self.bus.recv(timeout=0)
                    if stale is None:
                        break
                    try:
                        self._logger.info(
                            "FLUSH <- COB=0x%03X data=%s", stale.arbitration_id, stale.data.hex()
                        )
                    except Exception:
                        pass

                msg = can.Message(
                    arbitration_id=self._cob_id_tx(),
                    data=data,
                    is_extended_id=False,
                )
                try:
                    self._logger.info("TX   -> COB=0x%03X data=%s", msg.arbitration_id, msg.data.hex())
                except Exception:
                    self._logger.info("TX   -> COB=0x%03X (binary data)", msg.arbitration_id)

                self.bus.send(msg)

                deadline = time.monotonic() + self.config.sdo_timeout
                last_reply = None
                while time.monotonic() < deadline:
                    # First consult any messages the heartbeat thread stashed.
                    # Keep the receive path serialized so the heartbeat listener
                    # does not race with SDO replies on the same bus.
                    try:
                        reply = self._rx_backlog.get_nowait()
                    except Exception:
                        remaining = deadline - time.monotonic()
                        reply = self.bus.recv(timeout=max(0.0, remaining))
                    if reply is None:
                        break
                    last_reply = reply
                    try:
                        self._logger.info("RX   <- COB=0x%03X data=%s", reply.arbitration_id, reply.data.hex())
                    except Exception:
                        self._logger.info("RX   <- COB=0x%03X (binary data)", reply.arbitration_id)
                    if reply.arbitration_id == self._cob_id_rx():
                        return reply

            if attempt < retries:
                time.sleep(0.1)
                continue

            extra = ""
            if last_reply is not None:
                try:
                    data_hex = last_reply.data.hex()
                except Exception:
                    data_hex = str(last_reply.data)
                extra = f" Dernière trame reçue: COB=0x{last_reply.arbitration_id:03X}, data={data_hex}"
            self._logger.error("SDO timeout for node %s.%s", self.config.node_id, extra)
            raise TimeoutError(
                f"Pas de réponse SDO du nœud {self.config.node_id} "
                f"(COB-ID 0x{self._cob_id_rx():03X}).{extra}"
            )

    @staticmethod
    def _check_abort(reply: "can.Message", index: int, subindex: int) -> None:
        if reply.data[0] == SDO_ABORT:
            abort_code = struct.unpack("<I", reply.data[4:8])[0]
            raise SdoError(index, subindex, abort_code)

    def sdo_read_u16(self, index: int, subindex: int = 0x00) -> int:
        """Expedited SDO upload (read) of an UNSIGNED16 value."""
        request = struct.pack("<BHBxxxx", SDO_READ_REQUEST, index, subindex)
        reply = self._send_and_wait(request)
        self._check_abort(reply, index, subindex)
        # Accept both 2-byte and 4-byte expedited read acks, we only care
        # about the low 16 bits which matches how this sensor reports
        # heartbeat / filter values.
        value = struct.unpack("<H", reply.data[4:6])[0]
        return value

    def sdo_write_u16(self, index: int, subindex: int, value: int) -> None:
        """Expedited SDO download (write) of an UNSIGNED16 value."""
        request = struct.pack("<BHBHxx", SDO_WRITE_2BYTE, index, subindex, value)
        reply = self._send_and_wait(request)
        self._check_abort(reply, index, subindex)
        if reply.data[0] != SDO_WRITE_ACK:
            raise RuntimeError(
                f"Réponse SDO inattendue en écriture 0x{index:04X}:{subindex:02X} : "
                f"{reply.data.hex()}"
            )

    def sdo_write_ascii4(self, index: int, subindex: int, text4: str) -> None:
        """Expedited SDO download (write) of a 4 character ASCII signature
        (used for the CiA-301 'save'/'load' store & restore commands)."""
        if len(text4) != 4:
            raise ValueError("text4 doit contenir exactement 4 caractères")
        payload = text4.encode("ascii")
        request = struct.pack("<BHB4s", SDO_WRITE_4BYTE, index, subindex, payload)
        reply = self._send_and_wait(request)
        self._check_abort(reply, index, subindex)
        if reply.data[0] != SDO_WRITE_ACK:
            raise RuntimeError(
                f"Réponse SDO inattendue en écriture 0x{index:04X}:{subindex:02X} : "
                f"{reply.data.hex()}"
            )

    # ------------------------------------------------------------------ #
    # High level helpers matching the vendor notes
    # ------------------------------------------------------------------ #
    def read_heartbeat_ms(self) -> int:
        return self.sdo_read_u16(0x1017, 0x00)

    def write_heartbeat_ms(self, value_ms: int) -> None:
        self.sdo_write_u16(0x1017, 0x00, value_ms)

    def read_filter(self) -> int:
        return self.sdo_read_u16(0x2603, 0x00)

    def write_filter(self, value: int) -> None:
        self.sdo_write_u16(0x2603, 0x00, value)

    def save_parameters(self) -> None:
        self.sdo_write_ascii4(0x1010, 0x01, "save")

    def restore_defaults(self) -> None:
        self.sdo_write_ascii4(0x1011, 0x01, "load")

    # ------------------------------------------------------------------ #
    # Heartbeat listener (COB-ID 0x700 + NodeID), cyclic once configured
    # ------------------------------------------------------------------ #
    def _start_heartbeat_listener(self) -> None:
        self._heartbeat_listener_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat_listener(self) -> None:
        self._heartbeat_listener_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
            self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        cob_id = 0x700 + self.config.node_id
        last_ts: Optional[float] = None
        while not self._heartbeat_listener_stop.is_set():
            if self.bus is None:
                time.sleep(0.05)
                continue
            try:
                with self._lock:
                    msg = self.bus.recv(timeout=0.2)
            except Exception:
                continue
            if msg is not None:
                if msg.arbitration_id == cob_id:
                    now = time.monotonic()
                    if last_ts is not None:
                        period_ms = (now - last_ts) * 1000.0
                        self.last_heartbeat_period_ms = period_ms
                        if self.on_heartbeat is not None:
                            self.on_heartbeat(period_ms)
                    last_ts = now
                else:
                    # not a heartbeat frame — stash it so the SDO sender can
                    # consume it (avoid losing replies)
                    try:
                        with self._lock:
                            self._rx_backlog.put_nowait(msg)
                    except Exception:
                        pass
