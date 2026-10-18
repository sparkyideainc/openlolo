"""Box-page sessions live in the journal database and survive a restart."""

import pytest

from openlolo.adapters.storage.sqlite import Journal
from openlolo.interfaces import setup_portal
from openlolo.interfaces.setup_portal import PortalSessions


class Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def path(tmp_path):
    return tmp_path / "openlolo.sqlite3"


def rows(journal):
    return journal.db.execute("SELECT digest, last_used FROM portal_sessions").fetchall()


def test_session_survives_restart(path):
    clock = Clock()
    journal = Journal(path)
    token = PortalSessions(journal.db, clock).issue()
    journal.close()
    reopened = Journal(path)
    assert PortalSessions(reopened.db, clock).check(token) is True
    assert PortalSessions(reopened.db, clock).check("not-a-token") is False
    assert PortalSessions(reopened.db, clock).check(None) is False


def test_unused_session_never_expires(path):
    clock = Clock()
    journal = Journal(path)
    sessions = PortalSessions(journal.db, clock)
    old = sessions.issue()
    clock.now += 5 * 365 * 86_400
    assert sessions.check(old) is True
    sessions.issue()
    assert len(rows(journal)) == 2 and sessions.check(old) is True


def test_sessions_from_before_keep_working(path):
    clock = Clock()
    journal = Journal(path)
    journal.db.executescript(
        "DROP TABLE portal_sessions;"
        "CREATE TABLE portal_sessions (digest TEXT PRIMARY KEY, expires_at REAL NOT NULL);"
    )
    journal.db.execute(
        "INSERT INTO portal_sessions VALUES (?,?)", (setup_portal._digest("old"), clock.now + 30 * 86_400)
    )
    journal.db.commit()
    journal.close()
    reopened = Journal(path)
    assert [row["last_used"] for row in rows(reopened)] == [clock.now]
    assert PortalSessions(reopened.db, clock).check("old") is True


def test_use_renews_the_session_at_most_once_an_hour(path):
    clock = Clock()
    journal = Journal(path)
    sessions = PortalSessions(journal.db, clock)
    token = sessions.issue()
    start = journal.db.total_changes
    for _ in range(100):  # A busy hour of page requests: reads only.
        clock.now += 30
        assert sessions.renew(token) is False
    assert journal.db.total_changes == start
    clock.now += setup_portal.SESSION_RENEW_SECONDS
    assert sessions.renew(token) is True
    assert journal.db.total_changes == start + 1
    assert rows(journal)[0]["last_used"] == clock.now
    assert sessions.renew(token) is False


def test_pairing_a_different_phone_ends_every_session(path):
    clock = Clock()
    journal = Journal(path)
    sessions = PortalSessions(journal.db, clock)
    setup = sessions.issue()
    assert journal.record_owner("phone-a") is False  # First pairing: the setup session stays.
    assert journal.record_owner("phone-a") is False  # Same phone again (new transport): stays.
    assert sessions.check(setup) is True
    assert journal.record_owner("phone-b") is True
    assert rows(journal) == [] and sessions.check(setup) is False
    journal.close()
    assert Journal(path).record_owner("phone-b") is False  # Remembered across restarts.


def test_revoke_removes_the_row(path):
    journal = Journal(path)
    sessions = PortalSessions(journal.db, Clock())
    token = sessions.issue()
    sessions.revoke(token)
    sessions.revoke(None)
    assert rows(journal) == [] and sessions.check(token) is False


def test_cap_drops_the_least_recently_used(path):
    clock = Clock()
    journal = Journal(path)
    sessions = PortalSessions(journal.db, clock)
    tokens = []
    for _ in range(setup_portal.SESSION_LIMIT + 3):
        tokens.append(sessions.issue())
        clock.now += 1
    assert len(rows(journal)) == setup_portal.SESSION_LIMIT
    assert not any(sessions.check(t) for t in tokens[:3])
    assert all(sessions.check(t) for t in tokens[3:])
    clock.now += setup_portal.SESSION_RENEW_SECONDS
    assert sessions.renew(tokens[3]) is True  # The oldest, used again, is kept over newer ones.
    sessions.issue()
    assert sessions.check(tokens[3]) is True and sessions.check(tokens[4]) is False


def test_token_never_stored(path):
    journal = Journal(path)
    token = PortalSessions(journal.db, Clock()).issue()
    journal.close()
    assert token.encode() not in path.read_bytes()
    for name in ("openlolo.sqlite3-wal", "openlolo.sqlite3-shm"):
        side = path.with_name(name)
        assert not side.exists() or token.encode() not in side.read_bytes()
    stored = rows(Journal(path))
    assert [row["digest"] for row in stored] == [setup_portal._digest(token)]
