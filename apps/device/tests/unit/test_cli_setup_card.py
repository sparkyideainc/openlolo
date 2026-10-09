import json
import re
from pathlib import Path

import pytest
from support import LAN_CERT, LAN_KEY

from openlolo.bootstrap import Runtime
from openlolo.config import load_config
from openlolo.interfaces.cli import main


@pytest.fixture
def box(tmp_path):
    path = tmp_path / "box.toml"
    path.write_text(
        "backend = 'simulated'\n"
        f"state_dir = '{tmp_path / 'state'}'\n"
        f"runtime_dir = '{tmp_path / 'run'}'\n"
        "lan_url = 'https://openlolo-test.local:8443'\n"
        f"lan_cert = '{LAN_CERT}'\n"
        f"lan_key = '{LAN_KEY}'\n"
    )
    runtime = Runtime(load_config(path))  # Creates the state dir and the journal.
    runtime.journal.close()
    return path


def run(monkeypatch, capsys, *args):
    monkeypatch.setattr("sys.argv", ["openlolo", "--config", str(args[0]), *args[1:]])
    main()
    return capsys.readouterr()


def stored_password(box):
    return (Path(box).parent / "state" / "setup-ap-password").read_text().strip()


def card(stdout):
    lines = stdout.splitlines()
    assert lines[0].startswith("Wi-Fi name (SSID): OpenLolo-")
    password = lines[1].removeprefix("Wi-Fi password: ")
    assert re.fullmatch(r"[a-z0-9]{4}(-[a-z0-9]{4}){3}", password) and not set(password) & set("0o1li5s")
    assert lines[2].startswith("Setup page: http://openlolo-") and ".local" in lines[2]
    return password


def test_setup_card_text_only_prints_the_card(box, monkeypatch, capsys):
    out = run(monkeypatch, capsys, box, "setup-card", "--text-only")
    assert "█" not in out.out and "▄" not in out.out
    assert card(out.out) == stored_password(box)
    assert run(monkeypatch, capsys, box, "setup-card", "--text-only").out == out.out  # Stable card.


def test_setup_card_prints_wifi_and_page_qr_codes(box, monkeypatch, capsys):
    out = run(monkeypatch, capsys, box, "setup-card")
    assert card(out.out) == stored_password(box)
    lines = out.out.splitlines()
    titles = [
        line for line in lines if line.startswith(("1. Join the setup Wi-Fi", "2. Open the setup page"))
    ]
    assert len(titles) == 2 and ".local" in titles[1]
    assert sum(1 for line in lines if line.startswith("█▀▀▀▀▀▀▀█")) >= 2  # a finder row per code


def test_setup_card_rotate_replaces_the_password(box, monkeypatch, capsys):
    first = card(run(monkeypatch, capsys, box, "setup-card", "--text-only").out)
    rotated = card(run(monkeypatch, capsys, box, "setup-card", "--rotate", "--text-only").out)
    assert rotated != first and stored_password(box) == rotated
    assert card(run(monkeypatch, capsys, box, "setup-card", "--text-only").out) == rotated


def test_clients_lists_oauth_clients_only(box, monkeypatch, capsys):
    out = run(monkeypatch, capsys, box, "clients", "list")
    listed = json.loads(out.out)
    assert listed == {"clients": []}  # No paired-phone list: app keys left with the Bluetooth channel.
    with pytest.raises(SystemExit):
        run(monkeypatch, capsys, box, "clients", "revoke", "0123456789abcdef")
