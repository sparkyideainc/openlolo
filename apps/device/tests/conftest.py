import pytest

from openlolo.bootstrap import Runtime
from openlolo.config import Config


def pytest_configure(config):
    # pytest creates basetemp itself, but a fresh checkout has no ignored .tmp parent.
    (config.rootpath / ".tmp").mkdir(exist_ok=True)


@pytest.fixture
async def runtime(tmp_path):
    config = Config(
        backend="simulated",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    runtime = Runtime(config)
    await runtime.start()
    yield runtime
    await runtime.close()


@pytest.fixture
async def lease(runtime):
    return (await runtime.phone.acquire_control("owner"))["lease_token"]
