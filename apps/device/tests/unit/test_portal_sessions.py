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
    return journal.db.execute("SELECT digest, expires_at FROM portal_sessions").fetchall()


def test_session_survives_restart(path):
    clock = Clock()
    journal = Journal(path)
    token = PortalSessions(journal.db, clock).issue()
    journal.close()
    reopened = Journal(path)
    assert PortalSessions(reopened.db, clock).check(token) is True
    assert PortalSessions(reopened.db, clock).check("not-a-token") is False
    assert PortalSessions(reopened.db, clock).check(None) is False


def test_expired_session_fails_and_is_purged_at_next_issue(path):
    clock = Clock()
    journal = Journal(path)
    sessions = PortalSessions(journal.db, clock)
    old = sessions.issue()
    clock.now += setup_portal.SESSION_SECONDS
    assert sessions.check(old) is False
    assert len(rows(journal)) == 1  # check never writes
    fresh = sessions.issue()
    assert len(rows(journal)) == 1 and sessions.check(fresh) is True


def test_revoke_removes_the_row(path):
    journal = Journal(path)
    sessions = PortalSessions(journal.db, Clock())
    token = sessions.issue()
    sessions.revoke(token)
    sessions.revoke(None)
    assert rows(journal) == [] and sessions.check(token) is False


def test_cap_drops_the_oldest(path):
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
