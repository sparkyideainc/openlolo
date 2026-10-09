import os
from pathlib import Path

from openlolo.adapters.device.coredevice import CoreDeviceClient
from openlolo.adapters.device.simulated import SimulatedDevice
from openlolo.adapters.storage.binding import BindingStore
from openlolo.adapters.storage.sqlite import Journal
from openlolo.adapters.system.fake import FakeNetwork
from openlolo.adapters.system.network import NetworkManager, SystemHealth
from openlolo.adapters.system.setup_ap import ensure_setup_identity
from openlolo.application.autopair import AutoPair
from openlolo.application.calibration import Calibration
from openlolo.application.coordinator import Coordinator
from openlolo.application.phone import Phone
from openlolo.application.provisioning import Provisioning
from openlolo.domain.models import Binding
from openlolo.domain.profiles import ProfileStore
from openlolo.interfaces.auth import Auth


class Runtime:
    def __init__(self, config):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        ensure_setup_identity(config)
        self.journal = Journal(config.state_dir / "openlolo.sqlite3")
        self.bindings = BindingStore(config.state_dir)
        device = SimulatedDevice() if config.backend == "simulated" else CoreDeviceClient(config)
        if config.backend == "simulated" and self.bindings.read() is None:
            self.bindings.write(
                Binding(udid="simulated-phone", transport="usb", owner_confirmed=True, portrait_locked=True)
            )
        self.coordinator = Coordinator(self.journal, device, config.queue_size, config.lease_seconds)
        self.calibration = Calibration(config, self.journal)
        if config.backend == "simulated":
            self.network: FakeNetwork | NetworkManager = FakeNetwork(["home"])
        else:
            manager = NetworkManager(config.wifi_interface)
            manager.configure_setup_ap(config)
            self.network = manager
        self.auth = Auth(self.journal)
        self.provisioning = Provisioning(
            config, self.network, button_flag=config.runtime_dir / "setup-button"
        )
        self.profiles = ProfileStore(config.state_dir / "apps")
        self.phone = Phone(
            config,
            device,
            self.network if config.backend != "simulated" else SystemHealth(),
            self.journal,
            self.bindings,
            self.coordinator,
            self.calibration,
            provisioning=self.provisioning,
            profiles=self.profiles,
        )
        self.autopair = self.phone.autopair = AutoPair(
            config, self.phone, self.provisioning, self.bindings, self.journal
        )
        self.door_provider, self.door_gateway = build_door(self)
        self.lock = None

    async def start(self):
        import fcntl

        self.config.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.lock = (self.config.runtime_dir / "api.lock").open("w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            self.lock = None
            raise RuntimeError("Another OpenLolo API owns this runtime directory") from None
        if self.config.backend == "simulated":
            binding = self.bindings.read()
            if binding is not None:
                await self.phone.device.connect(binding)
        await self.coordinator.start()
        self.door_gateway.start()
        if self.config.backend != "simulated":
            await self.network.remove_legacy_hotspot()
            self.provisioning.start()
            self.autopair.start()

    async def close(self):
        await self.autopair.close()
        await self.provisioning.close()
        await self.door_gateway.close()
        await self.coordinator.close()
        self.journal.close()
        if self.lock:
            self.lock.close()


def build_door(runtime):
    """Build the OAuth provider and gateway for consents, clients, and the MCP bridge."""
    from openlolo.interfaces.mcp_http import Gateway, OAuthProvider
    from openlolo.interfaces.mcp_server import ALL_SCOPES

    config = runtime.config
    issuer = config.public_url or config.lan_url or "https://openlolo.invalid"
    provider = OAuthProvider(
        runtime.journal, runtime.auth, issuer, scopes=ALL_SCOPES, legacy_issuer=config.public_url
    )
    return provider, Gateway(runtime, provider)


def default_config_path():
    return Path(os.environ.get("OPENLOLO_CONFIG", "/etc/openlolo/box.toml"))
