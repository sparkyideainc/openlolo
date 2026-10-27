import pytest

from openlolo.application import fullpage, provisioning
from openlolo.bootstrap import Runtime
from openlolo.config import Config


def pytest_configure(config):
    # pytest creates basetemp itself, but a fresh checkout has no ignored .tmp parent.
    (config.rootpath / ".tmp").mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    # Real-time waits sized for a phone; the simulator answers at once.
    monkeypatch.setattr(fullpage, "LAG_MAX", 0.3)
    monkeypatch.setattr(fullpage, "PROBE_INTERVAL", 0.01)
    monkeypatch.setattr(fullpage, "STABLE_MAX", 0.3)
    monkeypatch.setattr(provisioning, "HANDOFF_READ_SECONDS", 0.01)
    monkeypatch.setattr(provisioning, "HANDOFF_SECONDS", 0.01)


@pytest.fixture
async def runtime(tmp_path):
    config = Config(
        backend="simulated",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        allowed_hosts=["testserver"],
    )
    runtime = Runtime(config)
    await runtime.start()
    yield runtime
    await runtime.close()


@pytest.fixture
async def lease(runtime):
    return (await runtime.phone.acquire_control("owner"))["lease_token"]
