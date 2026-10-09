import hashlib
import secrets
import time

from openlolo.domain.errors import OpenLoloError


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, journal):
        self.db = journal.db
        self.failures = []

    def issue(self):
        token = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM logins WHERE expires<?", (time.time(),))
        self.db.execute("INSERT INTO logins VALUES (?,?)", (digest(token), time.time() + 300))
        self.db.commit()
        return token

    def verify(self, token):
        """Consume a single-use credential as proof of owner presence without opening a session."""
        now = time.time()
        self.failures = [t for t in self.failures if t > now - 60]
        if len(self.failures) >= 10:
            raise OpenLoloError("LOGIN_RATE_LIMIT", status=429)
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            self.failures.append(now)
            raise OpenLoloError("INVALID_CREDENTIAL", status=401)
        with self.db:
            row = self.db.execute(
                "DELETE FROM logins WHERE digest=? AND expires>? RETURNING digest", (digest(token), now)
            ).fetchone()
        if not row:
            self.failures.append(now)
            raise OpenLoloError("INVALID_CREDENTIAL", status=401)

    def login(self, token):
        self.verify(token)
        now = time.time()
        session = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM sessions WHERE expires<?", (now,))
        self.db.execute("INSERT INTO sessions VALUES (?,?)", (digest(session), now + 8 * 3600))
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
