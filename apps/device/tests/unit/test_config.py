import pytest

from openlolo.config import Config, load_config, public_origin


def test_public_url_normalized_and_optional():
    assert Config(backend="simulated").public_url is None
    config = Config(backend="simulated", public_url="https://Pi.Tailnet.ts.net/")
    assert config.public_url == "https://pi.tailnet.ts.net"
    assert config.mcp_port == 8090
    assert public_origin("https://box.example.com:8443") == "https://box.example.com:8443"
    assert public_origin("https://box.example.com:443") == "https://box.example.com"


@pytest.mark.parametrize(
    "value",
    [
        "http://pi.tailnet.ts.net",
        "https://pi.tailnet.ts.net/mcp",
        "https://pi.tailnet.ts.net?x=1",
        "https://pi.tailnet.ts.net#frag",
        "https://user:pw@pi.tailnet.ts.net",
        "pi.tailnet.ts.net",
        "",
    ],
)
def test_public_url_rejects_non_origins(value):
    with pytest.raises(ValueError):
        Config(backend="simulated", public_url=value)


def test_mcp_port_must_not_collide():
    with pytest.raises(ValueError):
        Config(backend="simulated", public_url="https://pi.tailnet.ts.net", mcp_port=8090, portal_port=8090)
    with pytest.raises(ValueError):
        Config(backend="simulated", host="0.0.0.0", public_url="https://pi.tailnet.ts.net")


def test_unreadable_binding_reads_as_none(tmp_path):
    from openlolo.adapters.storage.binding import BindingStore

    (tmp_path / "binding.json").write_text(
        '{"usb_identity":"old-phone","owner_confirmed":true,"portrait_locked":true}'
    )
    assert BindingStore(tmp_path).read() is None


def test_lan_url_normalized_with_derived_defaults(tmp_path):
    config = Config(backend="simulated", state_dir=tmp_path, lan_url="https://OpenLolo-AB12.local:8443/")
    assert config.lan_url == "https://openlolo-ab12.local:8443"
    assert config.lan_port == 8443
    assert config.lan_cert == tmp_path / "tls" / "lan.crt"
    assert config.lan_key == tmp_path / "tls" / "lan.key"
    assert config.lan_files == (config.lan_cert, config.lan_key)
    assert config.setup_name == "OpenLolo-AB12"
    assert config.box_id == "ab12"
    assert Config(backend="simulated", setup_name="OpenLolo-CD34").box_id == "cd34"  # No lan_url needed.
    assert Config(backend="simulated").box_id is None
    assert Config(backend="simulated", setup_name="Mine").box_id is None
    assert config.lan_hosts == ["openlolo-ab12.local:8443"]
    assert config.lan_bind == "0.0.0.0"
    with_ip = Config(
        backend="simulated", lan_url="https://openlolo-ab12.local:8443", lan_extra_hosts=["192.168.4.25"]
    )
    assert with_ip.lan_hosts == ["openlolo-ab12.local:8443", "192.168.4.25:8443"]


def test_lan_defaults_when_disabled_and_port_443():
    config = Config(backend="simulated")
    assert config.lan_url is None and config.lan_hosts == [] and config.setup_name is None
    with pytest.raises(ValueError):
        config.lan_files
    explicit = Config(
        backend="simulated", lan_url="https://box.local", lan_cert="/x/c.pem", lan_key="/x/k.pem"
    )
    assert explicit.lan_port == 443
    assert explicit.lan_hosts == ["box.local"]
    assert explicit.setup_name == "OpenLolo-BOX"  # First host label when there is no "-suffix".
    assert Config(backend="simulated", lan_url="https://openlolo").setup_name == "OpenLolo"
    assert Config(backend="simulated", lan_url="https://x.local", setup_name="Mine").setup_name == "Mine"
    assert str(explicit.lan_cert) == "/x/c.pem" and str(explicit.lan_key) == "/x/k.pem"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lan_url": "https://openlolo-ab12.local:8080", "portal_port": 8080},  # box page port
        {"lan_url": "https://openlolo-ab12.local:8090", "public_url": "https://pi.tailnet.ts.net"},
        {"lan_url": "https://openlolo-ab12.local:8443", "lan_bind": "8.8.8.8"},
        {"lan_url": "https://openlolo-ab12.local:8443", "lan_extra_hosts": ["192.168.4.25:8443"]},
        {"lan_url": "http://openlolo-ab12.local:8443"},
        {"lan_url": "https://openlolo-ab12.local:8443", "setup_name": "x" * 21},  # SSID/hostname stem limit
    ],
)
def test_lan_rejections(kwargs):
    with pytest.raises(ValueError):
        Config(backend="simulated", **kwargs)


def test_lan_port_may_equal_unused_mcp_port():
    config = Config(backend="simulated", lan_url="https://openlolo-ab12.local:8090")
    assert config.lan_port == config.mcp_port == 8090


@pytest.mark.parametrize("name", ["", "wlan 0", "a" * 16, "wl/an0"])
def test_wifi_interface_validation(name):
    with pytest.raises(ValueError):
        Config(backend="simulated", wifi_interface=name)
    assert Config(backend="simulated", wifi_interface="wlp2s0").wifi_interface == "wlp2s0"


def test_board_profile_defaults_to_auto_and_loads_from_box_toml(tmp_path):
    assert Config(backend="simulated").board == "auto"
    path = tmp_path / "box.toml"
    path.write_text('backend = "simulated"\nboard = "radxa-zero3w"\n')
    assert load_config(path).board == "radxa-zero3w"
    with pytest.raises(ValueError):
        Config.model_validate({"backend": "simulated", "board": "orange-pi"})


def test_update_repo_defaults_and_rejects_paths():
    assert Config(backend="simulated").update_repo == "sparkyideainc/openlolo"
    assert Config(backend="simulated", update_repo="you/fork").update_repo == "you/fork"
    for value in ("../../etc", "you/fork/extra", "you", "you/fo rk"):
        with pytest.raises(ValueError):
            Config(backend="simulated", update_repo=value)
