from uuid import uuid4

import pytest

from openlolo.adapters.storage.sqlite import Journal
from openlolo.domain.errors import OpenLoloError


def test_duplicates_privacy_and_crash(tmp_path):
    path = tmp_path / "journal.sqlite3"
    j = Journal(path)
    op = str(uuid4())
    queued = str(uuid4())
    result, fresh = j.reserve(op, "owner", "type_text", {"text": "PRIVATE_TYPED_SENTENCE"})
    assert fresh
    assert j.reserve(op, "owner", "type_text", {"text": "PRIVATE_TYPED_SENTENCE"})[1] is False
    with pytest.raises(OpenLoloError):
        j.reserve(op, "owner", "type_text", {"text": "other"})
    with pytest.raises(OpenLoloError):
        j.reserve(op, "another", "type_text", {"text": "PRIVATE_TYPED_SENTENCE"})
    j.update(op, "DISPATCHED")
    j.reserve(queued, "owner", "tap", {})
    j.close()
    j = Journal(path)
    j.recover_interrupted()
    assert j.get(op, "owner")["state"] == "OUTCOME_UNKNOWN"
    assert j.get(queued, "owner")["state"] == "CANCELLED"
    j.close()
    assert b"PRIVATE_TYPED_SENTENCE" not in path.read_bytes()


def test_list_operations_pages_newest_first_with_client_labels(tmp_path):
    j = Journal(tmp_path / "journal.sqlite3")
    ids = [str(uuid4()) for _ in range(5)]
    for index, op in enumerate(ids):
        j.reserve(op, "browser-owner" if index % 2 else "agent-owner", "tap", {"n": index})
        # Deterministic, strictly increasing creation times for stable paging.
        j.db.execute(
            "UPDATE operations SET created=?, updated=? WHERE id=?", (100.0 + index, 100.0 + index, op)
        )
    j.db.commit()
    j.label_owner("agent-owner", "Claude Desktop")
    j.label_owner("agent-owner", "Claude Code")  # INSERT OR REPLACE keeps the latest label.

    listing = j.list_operations()
    assert [op["id"] for op in listing] == list(reversed(ids))
    assert set(listing[0]) == {"id", "kind", "state", "result", "created", "updated", "client"}
    assert "owner" not in listing[0] and "fingerprint" not in listing[0]
    assert [op["client"] for op in listing] == [
        "Claude Code",
        "unknown",
        "Claude Code",
        "unknown",
        "Claude Code",
    ]

    page = j.list_operations(limit=2)
    assert [op["id"] for op in page] == [ids[4], ids[3]]
    rest = j.list_operations(limit=2, before=page[-1]["created"])
    assert [op["id"] for op in rest] == [ids[2], ids[1]]
    assert [op["id"] for op in j.list_operations(limit=2, before=rest[-1]["created"])] == [ids[0]]
    assert j.list_operations(before=100.0) == []

    assert len(j.list_operations(limit=0)) == 1
    assert len(j.list_operations(limit=-5)) == 1
    assert len(j.list_operations(limit=10_000)) == 5
    j.close()


def test_page_operations_cursor_is_stable_under_duplicate_timestamps(tmp_path):
    j = Journal(tmp_path / "journal.sqlite3")
    ids = sorted(str(uuid4()) for _ in range(6))  # Sorted so the id tiebreak is predictable.
    for index, op in enumerate(ids):
        j.reserve(op, "agent-owner", "tap", {"n": index})
        # Three pairs of rows share one creation time; only (created, id) orders them.
        j.db.execute("UPDATE operations SET created=?, updated=? WHERE id=?", (100.0 + index // 2, 100.0, op))
    j.db.commit()
    j.label_owner("agent-owner", "Claude Desktop")
    j.update(ids[0], "FAILED", {"code": "QUEUE_FULL"})

    # Newest created first, then id descending: for each timestamp pair the larger id comes first.
    expected = []
    for created in (102.0, 101.0, 100.0):
        pair = [op for op in ids if 100.0 + ids.index(op) // 2 == created]
        expected += sorted(pair, reverse=True)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = j.page_operations(limit=2, cursor=cursor)
        pages += 1
        seen += [op["id"] for op in page["operations"]]
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
        assert cursor.endswith(":" + seen[-1])
    assert seen == expected and pages == 3  # Nothing skipped, nothing repeated.

    first = j.page_operations(limit=50)
    assert first["has_more"] is False and first["next_cursor"] is None
    row = next(op for op in first["operations"] if op["id"] == ids[0])
    assert row == {
        "id": ids[0],
        "kind": "tap",
        "state": "FAILED",
        "created": 100.0,
        "updated": row["updated"],
        "client": "Claude Desktop",
        "error_code": "QUEUE_FULL",
    }
    assert "result" not in first["operations"][0]

    # The byte budget ends a page early and points at the next row.
    budgeted = j.page_operations(limit=50, budget=200)
    assert 0 < len(budgeted["operations"]) < 6 and budgeted["has_more"] is True
    assert budgeted["next_cursor"].endswith(":" + budgeted["operations"][-1]["id"])

    # The old timestamp-only cursor still works as "strictly older than".
    older = j.page_operations(limit=50, cursor="101.0:")
    assert [op["id"] for op in older["operations"]] == expected[4:]

    with pytest.raises(OpenLoloError):
        j.page_operations(cursor="not-a-cursor")
    j.close()


def test_journal_revision_changes_on_every_write_and_after_restart(tmp_path):
    path = tmp_path / "journal.sqlite3"
    j = Journal(path)
    start = j.journal_revision()
    op = str(uuid4())
    j.reserve(op, "owner", "tap", {})
    after_reserve = j.journal_revision()
    assert after_reserve > start
    assert j.reserve(op, "owner", "tap", {})[1] is False and j.journal_revision() == after_reserve
    j.update(op, "SUCCEEDED", {"ok": True})
    assert j.journal_revision() > after_reserve
    j.label_owner("owner", "Claude")
    revision = j.journal_revision()
    j.close()
    j = Journal(path)
    assert j.journal_revision() != revision  # Seeded from the clock, so a restart is a change too.
    j.close()


def test_opens_old_schema_and_migrates_additively(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE operations (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
            created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE profiles (device TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE logins (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
        CREATE TABLE sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
        CREATE TABLE oauth_clients (
            client_id TEXT PRIMARY KEY, value TEXT NOT NULL, approved INTEGER NOT NULL,
            created REAL NOT NULL);
        CREATE TABLE oauth_grants (
            digest TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
            family TEXT NOT NULL, value TEXT NOT NULL, expires REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0);
        INSERT INTO secrets VALUES ('fingerprint', '00');
        INSERT INTO oauth_clients VALUES ('c1', '{}', 1, 1.0);
        INSERT INTO operations VALUES ('op-1', 'someone', 'tap', 'fp', 'SUCCEEDED', NULL, 1.0, 1.0);
    """)
    old.commit()
    old.close()

    for _ in range(2):  # Opening twice proves the migration is idempotent.
        j = Journal(path)
        columns = [row["name"] for row in j.db.execute("PRAGMA table_info(oauth_clients)")]
        assert columns == ["client_id", "value", "approved", "created", "issuer"]
        assert j.db.execute("SELECT issuer FROM oauth_clients WHERE client_id='c1'").fetchone()[0] is None
        assert j.db.execute("SELECT count(*) FROM owner_labels").fetchone()[0] == 0
        assert j.secret == b"\x00"
        assert [op["client"] for op in j.list_operations()] == ["unknown"]
        j.close()
