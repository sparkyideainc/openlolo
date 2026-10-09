"""USB and Wi-Fi transports. Each locates the explicitly bound phone and yields a tunnel provider.

Both are root-free: the USB path uses the CoreDeviceProxy lockdown service (iOS 17.4+), the
Wi-Fi path uses the RemotePairing service advertised over Bonjour. Neither ever pairs
implicitly; ``autopair`` is only enabled by the explicit owner setup in ``setup.py``.
"""

import asyncio
from pathlib import Path

from openlolo.domain.errors import OpenLoloError


def same_udid(left: str, right: str) -> bool:
    return left.replace("-", "").lower() == right.replace("-", "").lower()


class UsbTransport:
    name = "usb"

    def __init__(self, cache_dir: Path, poll_seconds: float = 2.0):
        self.cache_dir = cache_dir
        self.poll_seconds = poll_seconds

    async def devices(self) -> list:
        from pymobiledevice3 import usbmux

        try:
            return await asyncio.wait_for(usbmux.list_devices(), 3)
        except Exception:
            return []

    async def present(self, udid: str) -> bool:
        return any(device.is_usb and same_udid(device.serial, udid) for device in await self.devices())

    async def lockdown(self, udid: str, autopair: bool = False, pair_timeout: float | None = None):
        from pymobiledevice3.lockdown import create_using_usbmux

        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return await create_using_usbmux(
            serial=udid,
            connection_type="USB",
            autopair=autopair,
            pair_timeout=pair_timeout,
            pairing_records_cache_folder=self.cache_dir,
        )

    async def open(self, udid: str):
        from pymobiledevice3.exceptions import InvalidServiceError
        from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy

        lockdown = await asyncio.wait_for(self.lockdown(udid), 15)
        try:
            if not lockdown.paired:
                raise OpenLoloError("TRUST_REQUIRED", "Unlock the bound phone and run explicit owner pairing")
            try:
                provider = await asyncio.wait_for(CoreDeviceTunnelProxy.create(lockdown), 15)
            except InvalidServiceError:
                raise OpenLoloError(
                    "USB_TUNNEL_UNSUPPORTED", "iOS 17.4 or later is required for the USB CoreDevice tunnel"
                ) from None
        except BaseException:
            await lockdown.close()
            raise
        details = {"product_version": getattr(lockdown, "product_version", None)}
        return provider, [provider.close, lockdown.close], details

    def forget(self, udid: str | None = None):
        """Drop cached lockdown trust records; the phone shows the Trust prompt at the next pairing."""
        for path in self.cache_dir.glob("*.plist"):
            if udid is None or same_udid(path.stem, udid):
                path.unlink(missing_ok=True)

    async def verify(self, udid: str) -> dict:
        present = await self.present(udid)
        trusted = False
        if present:
            lockdown = await asyncio.wait_for(self.lockdown(udid), 15)
            try:
                trusted = bool(lockdown.paired)
            finally:
                await lockdown.close()
        return {"present": present, "trusted": trusted}


class WifiTransport:
    name = "wifi"

    def __init__(self, browse_seconds: float = 3.0, poll_seconds: float = 5.0):
        self.browse_seconds = browse_seconds
        self.poll_seconds = poll_seconds

    def records(self, udid: str) -> list[dict]:
        from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier

        return [
            record
            for identifier, _path, record in iter_remote_pair_records_by_identifier()
            if same_udid(identifier, udid)
        ]

    def paired(self, udid: str) -> bool:
        return bool(self.records(udid))

    def forget(self, udid: str | None = None):
        """Drop RemotePairing records so Wi-Fi mode needs a fresh USB pairing."""
        from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier

        for identifier, path, _record in list(iter_remote_pair_records_by_identifier()):
            if udid is None or same_udid(identifier, udid):
                path.unlink(missing_ok=True)

    async def present(self, udid: str) -> bool:
        from pymobiledevice3.bonjour import browse_remotepairing
        from pymobiledevice3.remote.siphash import validate_auth_tag
        from pymobiledevice3.remote.tunnel_service import PEER_ALT_IRK_KEY

        irks = [record[PEER_ALT_IRK_KEY] for record in self.records(udid) if record.get(PEER_ALT_IRK_KEY)]
        if not irks:
            return False
        try:
            answers = await browse_remotepairing(timeout=self.browse_seconds)
        except Exception:
            return False
        for answer in answers:
            identifier = answer.properties.get("identifier")
            tag = answer.properties.get("authTag")
            if identifier and tag and any(validate_auth_tag(irk, identifier, tag) for irk in irks):
                return True
        return False

    async def addresses(self, udid: str) -> list[str]:
        """IP addresses the paired phone advertises for RemotePairing right now (ADR 0012)."""
        from pymobiledevice3.bonjour import browse_remotepairing
        from pymobiledevice3.remote.siphash import validate_auth_tag
        from pymobiledevice3.remote.tunnel_service import PEER_ALT_IRK_KEY

        irks = [record[PEER_ALT_IRK_KEY] for record in self.records(udid) if record.get(PEER_ALT_IRK_KEY)]
        if not irks:
            return []
        try:
            answers = await browse_remotepairing(timeout=self.browse_seconds)
        except Exception:
            return []
        found: list[str] = []
        for answer in answers:
            identifier = answer.properties.get("identifier")
            tag = answer.properties.get("authTag")
            if identifier and tag and any(validate_auth_tag(irk, identifier, tag) for irk in irks):
                found.extend(str(address.ip) for address in answer.addresses)
        return sorted(set(found))

    async def open(self, udid: str):
        from pymobiledevice3.remote.tunnel_service import get_remote_pairing_tunnel_services

        if not self.paired(udid):
            raise OpenLoloError(
                "WIFI_PAIRING_REQUIRED", "Run explicit owner pairing over USB once before using Wi-Fi mode"
            )
        services = await get_remote_pairing_tunnel_services(bonjour_timeout=self.browse_seconds, udid=udid)
        if not services:
            raise OpenLoloError("DEVICE_DISCONNECTED", "The paired phone is not reachable over Wi-Fi")
        service, *extra = services
        for other in extra:
            await other.close()
        return service, [service.close], {}

    async def verify(self, udid: str) -> dict:
        return {"present": await self.present(udid), "trusted": self.paired(udid)}
