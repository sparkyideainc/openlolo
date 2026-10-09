"""Persistent CoreDevice worker: owns the tunnel and services, serves the API over a Unix socket."""

import argparse
import asyncio
import base64
import logging
import signal

from openlolo.adapters.storage.binding import BindingStore
from openlolo.config import load_config
from openlolo.coredevice import setup
from openlolo.coredevice.manager import CoreDeviceManager
from openlolo.coredevice.transports import WifiTransport
from openlolo.domain.errors import OpenLoloError
from openlolo.domain.models import Binding, CaptureOptions
from openlolo.workers.protocol import serve

BOUND_OPS = {
    "connect",
    "capture",
    "begin",
    "recover",
    "apps",
    "launch_app",
    "clipboard_get",
    "clipboard_set",
    "device_info",
}


class DeviceWorker:
    def __init__(self, config, manager=None):
        self.config = config
        self.bindings = BindingStore(config.state_dir)
        self.manager = manager or CoreDeviceManager(config)

    async def start(self):
        await self.manager.start()
        binding = self.bindings.read()
        if binding is not None:
            await self.manager.connect(binding)

    async def close(self):
        await self.manager.close()

    async def dispatch(self, request):
        op = request.get("op")
        if op == "status":
            return self.manager.status()
        if op == "release":
            await self.manager.release_all()
            return {"released": True}
        if op == "report":
            await self.manager.report(request["generation"], request["kind"], request["value"])
            return {"accepted": True}
        if op == "gesture":
            return await self.manager.gesture(request["generation"], request["points"])
        if op == "disconnect":
            return await self.manager.disconnect()
        if op == "unbind":
            self.bindings.delete()
            return await self.manager.unbind()
        if op == "discover":
            return await setup.discover(self.manager.transports["usb"], self.manager.transports["wifi"])
        if op == "usb_phones":
            return {"usb": await setup.usb_phones(self.manager.transports["usb"])}
        if op == "wifi_addresses":
            udid = request.get("udid")
            Binding(udid=udid, owner_confirmed=True, portrait_locked=True)  # Validates the identifier shape.
            wifi = self.manager.transports["wifi"]
            assert isinstance(wifi, WifiTransport)
            return {"addresses": await wifi.addresses(udid)}
        if op == "mount_ddi":
            return await self.manager.request_mount()
        if op in {"pair", "developer_mode"}:
            setup.require_owner(request)
            udid = request.get("udid")
            Binding(udid=udid, owner_confirmed=True, portrait_locked=True)  # Validates the identifier shape.
            usb = self.manager.transports["usb"]
            if op == "pair":
                return await setup.pair(usb, udid)
            return await setup.enable_developer_mode(usb, udid)
        if op == "verify":
            return await self.manager.verify(Binding.model_validate(request["binding"]))
        if op not in BOUND_OPS:
            raise OpenLoloError("UNKNOWN_OPERATION")
        binding = Binding.model_validate(request["binding"])
        if self.bindings.read() != binding:
            raise OpenLoloError("IDENTITY_MISMATCH", "The request does not match the persisted binding")
        if op == "connect":
            return await self.manager.connect(binding)
        if op == "capture":
            frame = await self.manager.capture(binding, CaptureOptions.model_validate(request["options"]))
            return {"metadata": frame.metadata.model_dump(), "image": base64.b64encode(frame.image).decode()}
        if op == "begin":
            return {"generation": await self.manager.begin(binding, request.get("kinds"))}
        if op == "recover":
            return await self.manager.recover(binding)
        if op == "apps":
            return {"apps": await self.manager.list_apps(binding)}
        if op == "launch_app":
            url = request.get("url")
            return await self.manager.launch_app(
                binding,
                str(request.get("bundle_id", "")),
                str(url) if url else None,
                bool(request.get("kill_existing", False)),
            )
        if op == "clipboard_get":
            return await self.manager.clipboard_get(binding)
        if op == "clipboard_set":
            return await self.manager.clipboard_set(binding, str(request.get("text", "")))
        return await self.manager.device_info(binding)


async def run(config):
    worker = DeviceWorker(config)
    await worker.start()
    task = asyncio.create_task(serve(config.runtime_dir / "device.sock", worker.dispatch))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await worker.close()


def main():
    parser = argparse.ArgumentParser(
        description="OpenLolo CoreDevice worker (USB, Wi-Fi or automatic transport)"
    )
    parser.add_argument("--config", default="/etc/openlolo/box.toml")
    parser.add_argument(
        "--verbose", action="store_true", help="Library diagnostics; never typed text or images"
    )
    args = parser.parse_args()
    # Warnings name failure classes only; --verbose adds library diagnostics that may include
    # identifiers, never typed text or image payloads.
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    if args.verbose:
        for name in ("pymobiledevice3.remote.tunnel_service", "pymobiledevice3.remote.remotexpc"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    asyncio.run(run(load_config(args.config)))


if __name__ == "__main__":
    main()
