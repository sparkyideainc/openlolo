import time

import pytest
from support import raises_code

from openlolo.adapters.storage.sqlite import Journal
from openlolo.interfaces.auth import SESSION_SECONDS, Auth


@pytest.fixture
def auth(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    yield Auth(journal)
    journal.close()


def test_open_session_and_logout(auth):
    session = auth.open_session()
    owner = auth.owner(session)
    assert owner == auth.owner(session) and len(owner) == 64
    assert auth.owner(auth.open_session()) != owner  # Every bridge is its own owner.
    auth.logout(session)
    with raises_code("LOGIN_REQUIRED"):
        auth.owner(session)
    with raises_code("LOGIN_REQUIRED"):
        auth.owner("")


def test_lapsed_session_is_refused(auth, monkeypatch):
    session = auth.open_session()
    later = time.time() + SESSION_SECONDS + 1
    monkeypatch.setattr("openlolo.interfaces.auth.time.time", lambda: later)
    with raises_code("LOGIN_REQUIRED"):
        auth.owner(session)


def test_old_app_keys_and_label_code_are_dropped_on_open(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    with journal.db:
        journal.db.execute("CREATE TABLE IF NOT EXISTS app_keys (app_id TEXT PRIMARY KEY)")
        journal.db.execute("INSERT INTO secrets VALUES ('setup_code', 'ABCDEFGHJKMN')")
        journal.db.execute("CREATE TABLE IF NOT EXISTS logins (digest TEXT PRIMARY KEY, expires REAL)")
    journal.close()
    reopened = Journal(path)
    try:
        tables = {r[0] for r in reopened.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "app_keys" not in tables and "logins" not in tables
        assert reopened.db.execute("SELECT COUNT(*) FROM secrets WHERE name='setup_code'").fetchone()[0] == 0
    finally:
        reopened.close()
