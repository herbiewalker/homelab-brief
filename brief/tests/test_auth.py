from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher

from brief import auth as a
from brief.store import Store

GOOD = "correct horse battery"
OTHER = "a different long passphrase"
FAST = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "baseline.db")
    auth = a.Auth(store, clock, FAST)
    auth.create_admin("admin", GOOD)
    yield auth, store, clock
    store.close()


def audit_actions(store):
    with store.read() as db:
        return [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY rowid")]


# ---------- passwords ----------


@pytest.mark.parametrize(
    "password, problem",
    [("short", "at least"), ("x" * 300, "at most"), ("admin-user-name", "username"), ("aaaaaaaaaaaaaaaa", "easy"),
     ("PasswordPassword", "easy")],
)  # fmt: skip
def test_weak_passwords_are_refused_with_a_reason(password, problem):
    assert problem in a.password_problem(password, "admin-user-name")


def test_length_is_what_counts_not_character_classes():
    assert a.password_problem("correct horse battery") is None
    assert a.password_problem("lowercaseonlywords") is None


def test_stored_hashes_are_argon2id_and_the_password_is_never_stored(tmp_path):
    store = Store(tmp_path / "x.db")
    auth = a.Auth(store)  # the real, default hasher
    auth.create_admin("admin", GOOD)
    with store.read() as db:
        stored = db.execute("SELECT password_hash FROM admin_user").fetchone()[0]
        dump = "\n".join(
            str(tuple(r))
            for t in ("admin_user", "audit_log", "meta")
            for r in db.execute(f"SELECT * FROM {t}")
        )
    assert stored.startswith("$argon2id$") and GOOD not in dump
    store.close()


# ---------- accounts ----------


def test_only_one_admin_can_be_created_and_its_creation_is_audited(env):
    auth, store, _ = env
    assert auth.has_admin()
    with pytest.raises(a.AuthError, match="already exists"):
        auth.create_admin("second", GOOD)
    assert audit_actions(store) == ["admin.created"]


def test_a_weak_password_cannot_create_the_admin(tmp_path):
    store = Store(tmp_path / "x.db")
    auth = a.Auth(store, hasher=FAST)
    with pytest.raises(a.AuthError, match="at least"):
        auth.create_admin("admin", "short")
    assert not auth.has_admin()
    store.close()


# ---------- signing in ----------


def test_a_good_login_opens_a_session_and_is_audited(env):
    auth, store, _ = env
    token, session = auth.login("admin", GOOD, "198.51.100.5")
    assert session.username == "admin" and len(token) >= 40 and session.csrf
    assert auth.session(token).username == "admin"
    assert "login.ok" in audit_actions(store)


def test_usernames_are_case_insensitive(env):
    auth, _, _ = env
    assert auth.login("ADMIN", GOOD, "198.51.100.5")[1].username == "admin"


def test_wrong_password_and_unknown_user_look_identical(env):
    auth, store, _ = env
    with pytest.raises(a.BadCredentials) as wrong:
        auth.login("admin", "not the password", "198.51.100.5")
    with pytest.raises(a.BadCredentials) as unknown:
        auth.login("nobody", "not the password", "198.51.100.6")
    assert str(wrong.value) == str(unknown.value) == a.GENERIC_FAILURE
    assert audit_actions(store).count("login.failed") == 2


def test_the_session_token_is_stored_only_as_a_hash(env):
    auth, store, _ = env
    token, _ = auth.login("admin", GOOD, "198.51.100.5")
    with store.read() as db:
        stored = [r[0] for r in db.execute("SELECT token_hash FROM session")]
    assert token not in stored and len(stored[0]) == 64


# ---------- throttling ----------


def fail(auth, n, user="admin", ip="198.51.100.5"):
    for _ in range(n):
        with pytest.raises(a.BadCredentials):
            auth.login(user, "wrong password here", ip)


def test_repeated_failures_lock_out_and_even_the_right_password_waits(env):
    auth, _, clock = env
    fail(auth, a.FAIL_FREE)
    with pytest.raises(a.Throttled) as lock:
        auth.login("admin", GOOD, "198.51.100.5")  # correct, but locked
    assert 0 < lock.value.retry_after <= a.LOCK_BASE_SECONDS + 1
    clock.advance(seconds=lock.value.retry_after + 1)
    assert auth.login("admin", GOOD, "198.51.100.5")[1].username == "admin"


def test_the_lock_grows_with_more_failures_up_to_a_cap(env):
    auth, _, clock = env
    fail(auth, a.FAIL_FREE)
    clock.advance(seconds=31)
    fail(auth, 1)  # a 6th failure
    with pytest.raises(a.Throttled) as lock:
        auth.login("admin", GOOD, "198.51.100.5")
    assert lock.value.retry_after > a.LOCK_BASE_SECONDS
    clock.advance(seconds=5)
    for _ in range(10):  # keep failing as soon as each lock ends
        wait = auth.throttle_remaining(a.Auth.scopes("198.51.100.5", "admin"))
        clock.advance(seconds=wait + 1)
        fail(auth, 1)
    assert auth.throttle_remaining(a.Auth.scopes("198.51.100.5", "admin")) <= a.LOCK_MAX_SECONDS


def test_one_address_trying_many_usernames_is_locked_by_address(env):
    auth, _, _ = env
    for i in range(a.FAIL_FREE):
        with pytest.raises(a.BadCredentials):
            auth.login(f"guess{i}", "wrong password here", "198.51.100.9")
    with pytest.raises(a.Throttled):
        auth.login("guess-new", "wrong password here", "198.51.100.9")


def test_many_addresses_trying_one_username_are_locked_by_username(env):
    auth, _, _ = env
    for i in range(a.FAIL_FREE):
        with pytest.raises(a.BadCredentials):
            auth.login("admin", "wrong password here", f"198.51.100.{i + 10}")
    with pytest.raises(a.Throttled):
        auth.login("admin", "wrong password here", "203.0.113.77")


def test_the_throttle_survives_a_restart(tmp_path):
    clock = Clock()
    path = tmp_path / "baseline.db"
    store = Store(path)
    auth = a.Auth(store, clock, FAST)
    auth.create_admin("admin", GOOD)
    fail(auth, a.FAIL_FREE)
    store.close()
    again = a.Auth(Store(path), clock, FAST)
    with pytest.raises(a.Throttled):
        again.login("admin", GOOD, "198.51.100.5")


def test_old_failures_age_out_and_success_resets_the_count(env):
    auth, _, clock = env
    fail(auth, a.FAIL_FREE - 1)
    clock.advance(minutes=20)
    fail(auth, a.FAIL_FREE - 1)  # the earlier ones no longer count
    auth.login("admin", GOOD, "198.51.100.5")
    fail(auth, a.FAIL_FREE - 1)  # and a success started the count over


def test_a_throttled_attempt_is_audited(env):
    auth, store, _ = env
    fail(auth, a.FAIL_FREE)
    with pytest.raises(a.Throttled):
        auth.login("admin", GOOD, "198.51.100.5")
    assert "login.throttled" in audit_actions(store)


# ---------- sessions ----------


def test_a_session_ends_after_30_idle_minutes_but_activity_extends_it(env):
    auth, _, clock = env
    token, _ = auth.login("admin", GOOD, "198.51.100.5")
    for _ in range(5):
        clock.advance(minutes=20)
        assert auth.session(token) is not None  # each visit pushes the idle timer
    clock.advance(minutes=31)
    assert auth.session(token) is None


def test_a_session_ends_after_12_hours_no_matter_what(env):
    auth, _, clock = env
    token, _ = auth.login("admin", GOOD, "198.51.100.5")
    for _ in range(25):
        clock.advance(minutes=29)
        alive = auth.session(token)
    assert alive is None


def test_garbage_and_missing_tokens_are_not_sessions(env):
    auth, _, _ = env
    assert auth.session(None) is None and auth.session("") is None and auth.session("not-a-token") is None


def test_logout_ends_the_session_and_is_audited(env):
    auth, store, _ = env
    token, session = auth.login("admin", GOOD, "198.51.100.5")
    auth.logout(token, session)
    assert auth.session(token) is None and "logout" in audit_actions(store)


def test_changing_the_password_ends_every_session(env):
    auth, store, _ = env
    token, _ = auth.login("admin", GOOD, "198.51.100.5")
    auth.set_password("admin", OTHER)
    assert auth.session(token) is None
    with pytest.raises(a.BadCredentials):
        auth.login("admin", GOOD, "198.51.100.5")
    assert auth.login("admin", OTHER, "198.51.100.5")
    assert "password.changed" in audit_actions(store)


def test_a_weak_new_password_is_refused_and_the_old_one_still_works(env):
    auth, _, _ = env
    with pytest.raises(a.AuthError):
        auth.set_password("admin", "short")
    assert auth.login("admin", GOOD, "198.51.100.5")


# ---------- the first-run setup token ----------


@pytest.fixture
def fresh(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "baseline.db")
    yield a.Auth(store, clock, FAST), store, clock
    store.close()


def test_the_setup_token_creates_the_admin_once(fresh):
    auth, store, _ = fresh
    token = auth.issue_setup_token()
    assert token and len(token) >= 24
    auth.setup_admin(token, "admin", GOOD, "198.51.100.5")
    assert auth.has_admin()
    assert auth.issue_setup_token() is None
    with pytest.raises(a.BadCredentials):
        auth.setup_admin(token, "other", GOOD, "198.51.100.5")  # the token is spent
    assert "admin.created" in audit_actions(store)


def test_the_setup_token_is_stored_only_as_a_hash(fresh):
    auth, store, _ = fresh
    token = auth.issue_setup_token()
    with store.read() as db:
        assert token not in "".join(r[0] for r in db.execute("SELECT value FROM meta"))


def test_a_wrong_or_expired_setup_token_is_refused_and_counts_toward_the_throttle(fresh):
    auth, _, clock = fresh
    token = auth.issue_setup_token()
    with pytest.raises(a.BadCredentials):
        auth.setup_admin("wrong", "admin", GOOD, "198.51.100.5")
    clock.advance(hours=25)
    with pytest.raises(a.BadCredentials):
        auth.setup_admin(token, "admin", GOOD, "198.51.100.5")
    assert not auth.has_admin()
    for _ in range(a.FAIL_FREE):
        try:
            auth.setup_admin("wrong", "admin", GOOD, "198.51.100.7")
        except a.AuthError:
            pass
    with pytest.raises(a.Throttled):
        auth.setup_admin("wrong", "admin", GOOD, "198.51.100.7")


def test_a_weak_password_does_not_use_up_the_setup_token(fresh):
    auth, _, _ = fresh
    token = auth.issue_setup_token()
    with pytest.raises(a.AuthError, match="at least"):
        auth.setup_admin(token, "admin", "short", "198.51.100.5")
    auth.setup_admin(token, "admin", GOOD, "198.51.100.5")
    assert auth.has_admin()


def test_each_restart_issues_a_new_token_and_the_old_one_stops_working(fresh):
    auth, _, _ = fresh
    first = auth.issue_setup_token()
    second = auth.issue_setup_token()
    with pytest.raises(a.BadCredentials):
        auth.setup_admin(first, "admin", GOOD, "198.51.100.5")
    auth.setup_admin(second, "admin", GOOD, "198.51.100.5")
