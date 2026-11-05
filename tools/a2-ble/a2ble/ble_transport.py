"""BLE transport: scan / connect / write / notify, via bleak.

GATT identifiers (as the official app uses them):

- Service:          0000FE3C-0000-1000-8000-00805f9b34fb
- Write (client->device):   0000FE1C-0000-1000-8000-00805f9b34fb
- Notify (device->client):  0000FE1B-0000-1000-8000-00805f9b34fb (CCCD 2902)

This module is importable without bleak installed (bleak is imported lazily
inside the class, not at module scope) so the rest of the package stays
testable on a host with no Bluetooth adapter -- such as the one this client
was built and tested on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

log = logging.getLogger("a2ble.ble")

SERVICE_UUID = "0000fe3c-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000fe1c-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000fe1b-0000-1000-8000-00805f9b34fb"


class TransportError(RuntimeError):
    pass


class BleTransport:
    """Thin async wrapper around bleak for this one service/characteristic pair."""

    def __init__(self):
        self._client = None
        self._on_notify: Optional[Callable[[bytes], None]] = None

    @staticmethod
    async def scan(timeout: float = 6.0):
        """Return BLEDevice-like objects advertising the QwenNote A2 service."""
        try:
            from bleak import BleakScanner
        except ImportError as exc:
            raise TransportError(
                "bleak is required for scanning; pip install -r requirements.txt") from exc
        devices = await BleakScanner.discover(timeout=timeout, service_uuids=[SERVICE_UUID])
        return [d for d in devices if _advertises_service(d, SERVICE_UUID)]

    async def connect(self, address: str, timeout: float = 10.0):
        try:
            from bleak import BleakClient
        except ImportError as exc:
            raise TransportError(
                "bleak is required to connect; pip install -r requirements.txt") from exc
        client = BleakClient(address, timeout=timeout)
        await client.connect()
        services = client.services if hasattr(client, "services") else await client.get_services()
        if services.get_characteristic(WRITE_CHAR_UUID) is None:
            await client.disconnect()
            raise TransportError("device at %s has no FE1C write characteristic" % address)
        if services.get_characteristic(NOTIFY_CHAR_UUID) is None:
            await client.disconnect()
            raise TransportError("device at %s has no FE1B notify characteristic" % address)
        self._client = client

    async def start_notify(self, on_notify: Callable[[bytes], None]) -> None:
        if self._client is None:
            raise TransportError("not connected")
        self._on_notify = on_notify

        def _handler(_sender, data: bytearray):
            self._on_notify(bytes(data))

        await self._client.start_notify(NOTIFY_CHAR_UUID, _handler)

    async def write(self, data: bytes, response: bool = False) -> None:
        if self._client is None:
            raise TransportError("not connected")
        await self._client.write_gatt_char(WRITE_CHAR_UUID, data, response=response)

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    @property
    def is_connected(self) -> bool:
        return bool(self._client is not None and getattr(self._client, "is_connected", True))


def _advertises_service(device, service_uuid: str) -> bool:
    uuids = []
    md = getattr(device, "metadata", None)
    if isinstance(md, dict):
        uuids = md.get("uuids") or []
    details = getattr(device, "details", None)
    adv = getattr(device, "advertisement", None)
    if adv is not None:
        uuids = list(uuids) + list(getattr(adv, "service_uuids", []) or [])
    return service_uuid.lower() in [u.lower() for u in uuids] if uuids else True
