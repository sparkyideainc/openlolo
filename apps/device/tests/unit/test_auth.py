import pytest
from support import raises_code

from openlolo.adapters.storage.sqlite import Journal
from openlolo.domain.errors import OpenLoloError
from openlolo.interfaces.auth import Auth


@pytest.fixture
def auth(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    yield Auth(journal)
    journal.close()


def test_single_use_credential_is_consumed_once(auth):
    token = auth.issue()
    auth.verify(token)
    with raises_code("INVALID_CREDENTIAL"):
        auth.verify(token)
    for bad in ("", "x" * 257, None, 12):
        with raises_code("INVALID_CREDENTIAL"):
            auth.verify(bad)  # type: ignore[arg-type]
    assert len(auth.failures) == 5


def test_ten_failures_lock_the_box_for_a_minute(auth):
    for _ in range(10):
        with raises_code("INVALID_CREDENTIAL"):
            auth.verify("not-a-credential")
    with pytest.raises(OpenLoloError) as info:
        auth.verify(auth.issue())  # Correct, but the box is locked out.
    assert info.value.code == "LOGIN_RATE_LIMIT" and info.value.status == 429


def test_login_opens_a_session_and_logout_ends_it(auth):
    session = auth.login(auth.issue())
    owner = auth.owner(session)
    assert owner == auth.owner(session) and len(owner) == 64
    auth.logout(session)
    with raises_code("LOGIN_REQUIRED"):
        auth.owner(session)
    with raises_code("LOGIN_REQUIRED"):
        auth.owner("")


def test_old_app_keys_and_label_code_are_dropped_on_open(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    with journal.db:
        journal.db.execute("CREATE TABLE IF NOT EXISTS app_keys (app_id TEXT PRIMARY KEY)")
        journal.db.execute("INSERT INTO secrets VALUES ('setup_code', 'ABCDEFGHJKMN')")
    journal.close()
    reopened = Journal(path)
    try:
        tables = {r[0] for r in reopened.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "app_keys" not in tables
        assert reopened.db.execute("SELECT COUNT(*) FROM secrets WHERE name='setup_code'").fetchone()[0] == 0
    finally:
        reopened.close()
