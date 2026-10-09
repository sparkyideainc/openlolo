"""Transports never pair implicitly and only ever address the explicitly bound phone."""

import sys
import types

from support import raises_code

from openlolo.coredevice.state import classify, transport_lost
from openlolo.coredevice.transports import UsbTransport, WifiTransport, same_udid


def install(monkeypatch, **modules):
    monkeypatch.setitem(sys.modules, "pymobiledevice3", types.ModuleType("pymobiledevice3"))
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


async def test_usb_transport_never_autopairs_and_reports_trust(monkeypatch, tmp_path):
    calls = []

    class Lockdown:
        paired = False
        product_version = "27.0"

        async def close(self):
            calls.append("closed")

    async def factory(**kwargs):
        calls.append(kwargs)
        return Lockdown()

    class Device:
        serial = "00008110-000A1B2C3D4E5F60"
        connection_type = "USB"
        is_usb = True

    async def list_devices():
        return [Device()]

    install(
        monkeypatch,
        **{
            "pymobiledevice3.lockdown": types.SimpleNamespace(create_using_usbmux=factory),
            "pymobiledevice3.usbmux": types.SimpleNamespace(list_devices=list_devices),
            "pymobiledevice3.exceptions": types.SimpleNamespace(
                InvalidServiceError=type("E", (Exception,), {})
            ),
            "pymobiledevice3.remote": types.ModuleType("pymobiledevice3.remote"),
            "pymobiledevice3.remote.tunnel_service": types.SimpleNamespace(CoreDeviceTunnelProxy=None),
        },
    )
    usb = UsbTransport(tmp_path / "lockdown")
    assert await usb.present("00008110000A1B2C3D4E5F60") and not await usb.present("other")
    with raises_code("TRUST_REQUIRED"):
        await usb.open(Device.serial)
    assert calls[0]["autopair"] is False and calls[0]["serial"] == Device.serial
    assert (
        calls[0]["connection_type"] == "USB"
        and calls[0]["pairing_records_cache_folder"] == tmp_path / "lockdown"
    )
    assert calls[-1] == "closed"
    assert (await usb.verify(Device.serial)) == {"present": True, "trusted": False}
    assert all(c["autopair"] is False for c in calls if isinstance(c, dict))


async def test_wifi_transport_requires_pair_record_and_matches_only_bound_phone(monkeypatch):
    records = [("00008110-AAAAAAAAAAAAAAAA", None, {"peer_alt_irk": b"irk"})]
    browsed = [
        types.SimpleNamespace(properties={"identifier": "svc", "authTag": "ok"}, addresses=[], port=1),
        types.SimpleNamespace(properties={}, addresses=[], port=1),
    ]
    requested = []

    def iter_records():
        yield from records

    async def browse(timeout):
        return browsed

    async def services(bonjour_timeout, udid):
        requested.append(udid)
        return []

    install(
        monkeypatch,
        **{
            "pymobiledevice3.pair_records": types.SimpleNamespace(
                iter_remote_pair_records_by_identifier=iter_records
            ),
            "pymobiledevice3.bonjour": types.SimpleNamespace(browse_remotepairing=browse),
            "pymobiledevice3.remote": types.ModuleType("pymobiledevice3.remote"),
            "pymobiledevice3.remote.siphash": types.SimpleNamespace(
                validate_auth_tag=lambda irk, identifier, tag: irk == b"irk" and tag == "ok"
            ),
            "pymobiledevice3.remote.tunnel_service": types.SimpleNamespace(
                PEER_ALT_IRK_KEY="peer_alt_irk", get_remote_pairing_tunnel_services=services
            ),
        },
    )
    wifi = WifiTransport(browse_seconds=0.1)
    assert await wifi.present("00008110AAAAAAAAAAAAAAAA")
    assert not await wifi.present("00008110-BBBBBBBBBBBBBBBB")
    with raises_code("WIFI_PAIRING_REQUIRED"):
        await wifi.open("00008110-BBBBBBBBBBBBBBBB")
    with raises_code("DEVICE_DISCONNECTED"):
        await wifi.open("00008110-AAAAAAAAAAAAAAAA")
    assert requested == ["00008110-AAAAAAAAAAAAAAAA"]


def test_error_classification_is_payload_free():
    class NotPairedError(Exception):
        pass

    class ConnectionTerminatedError(Exception):
        pass

    class CoreDeviceError(Exception):
        pass

    assert classify(NotPairedError("secret device text")).code == "TRUST_REQUIRED"
    assert classify(ConnectionTerminatedError()).code == "DEVICE_DISCONNECTED"
    assert classify(CoreDeviceError("device said: private")).as_dict() == {
        "code": "COREDEVICE_ERROR",
        "message": "The phone rejected the CoreDevice request",
    }
    assert classify(RuntimeError("x")).code == "BACKEND_FAILED"
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = ConnectionResetError()
    assert transport_lost(wrapped) and not transport_lost(ValueError())
    assert same_udid("00008110-AAAA", "00008110aaaa")
