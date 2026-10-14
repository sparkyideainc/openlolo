"""Sessions of the internal Phone API.

The API has no login of its own: it is reached only in-process, by the MCP gateway's per-client
bridges and the box page, each of which opens a session here after authenticating its caller
(OAuth bearer, box-page cookie). The session token is the owner identity for leases and the
journal; only its digest is stored.
"""

import hashlib
import secrets
import time

from openlolo.domain.errors import OpenLoloError

SESSION_SECONDS = 8 * 3600  # A bridge opens a new one when its session lapses.


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, journal):
        self.db = journal.db

    def open_session(self) -> str:
        now = time.time()
        session = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM sessions WHERE expires<?", (now,))
        self.db.execute("INSERT INTO sessions VALUES (?,?)", (digest(session), now + SESSION_SECONDS))
        self.db.commit()
        return session

    def owner(self, session):
        if not session:
            raise OpenLoloError("LOGIN_REQUIRED", status=401)
        owner = digest(session)
        row = self.db.execute("SELECT expires FROM sessions WHERE digest=?", (owner,)).fetchone()
        if not row or row[0] <= time.time():
            raise OpenLoloError("LOGIN_REQUIRED", status=401)
        return owner

    def logout(self, session):
        self.db.execute("DELETE FROM sessions WHERE digest=?", (digest(session),))
        self.db.commit()
