#!/usr/bin/env python3
"""End-to-end check of the web layer against the built image, over real TLS (Linux or WSL, Docker).

Starts a container with no admin account and walks the whole security story:
  - the HTTP side serves the status page and /healthz, and only redirects /admin to HTTPS;
  - the HTTPS side is a self-signed certificate, refuses /admin without a login, and rejects
    a sign-in without a CSRF token;
  - first-run setup works with the one-time token from the container log, and only once;
  - the session cookie is Secure, HttpOnly, and SameSite=Strict, and a write without the CSRF
    token is rejected;
  - `brief admin set-password` (the CLI, through docker exec) changes the password and ends sessions;
  - repeated failed sign-ins are throttled.

  python3 scripts/e2e_web.py [--image homelab-brief:dev] [--http-port 18080] [--https-port 18443]
"""

import argparse
import http.cookiejar
import json
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOOD = "correct horse battery staple"
CHANGED = "another long passphrase here"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the certificate is self-signed on purpose
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=context),
            NoRedirect,
        )

    def request(self, method, url, data=None, headers=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, data, **kw):
        return self.request("POST", url, data=data, **kw)


def docker(*args, check=True, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, **kw)


def csrf_in(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--image", default="homelab-brief:dev")
    ap.add_argument("--http-port", type=int, default=18080)
    ap.add_argument("--https-port", type=int, default=18443)
    args = ap.parse_args(argv)
    http_url, https_url = f"http://127.0.0.1:{args.http_port}", f"https://127.0.0.1:{args.https_port}"

    tmp = Path(tempfile.mkdtemp(prefix="hb-e2e-web-"))
    for d in ("state", "archive"):
        (tmp / d).mkdir()
        (tmp / d).chmod(0o777)
    name = "hb-e2e-web"
    docker("rm", "-f", name, check=False)
    docker("run", "-d", "--rm", "--name", name, "-e", "SYSLOG_PORT=5514", "-e", "BRIEF_INGEST_INTERVAL=1",
           "-e", f"HTTPS_PUBLIC_PORT={args.https_port}", "-v", f"{tmp}/state:/state", "-v", f"{tmp}/archive:/archive",
           "-v", f"{ROOT}/vector:/app/vector:ro", "-p", f"{args.http_port}:80", "-p", f"{args.https_port}:443",
           args.image)  # fmt: skip
    problems: list[str] = []

    def check(ok, message):
        if not ok:
            problems.append(message)
            print(f"[web] FAIL: {message}")

    try:
        anon = Client()
        status = None
        for _ in range(90):  # wait for TLS, the worker's first cycle, and the receiver
            try:
                status, _, body = anon.get(f"{https_url}/admin/login")
                code, _, health = anon.get(f"{http_url}/healthz")
                if status == 200 and code == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        check(status == 200, f"HTTPS never came up (last status {status})")

        # ---- HTTP side
        code, _, health = anon.get(f"{http_url}/healthz")
        check(code == 200 and json.loads(health)["status"] == "ok", f"/healthz was {code}: {health[:200]}")
        code, _, page = anon.get(f"{http_url}/")
        check(
            code == 200 and "homelab-brief" in page and "<script" not in page.lower(), "status page over HTTP"
        )
        code, headers, _ = anon.get(f"{http_url}/admin/login")
        check(
            code == 308 and headers["Location"] == f"{https_url}/admin/login",
            f"HTTP /admin redirect: {code} {headers.get('Location')}",
        )
        code, _, _ = anon.post(f"{http_url}/admin/login", {"username": "a", "password": "b"})
        check(code == 400, f"a POST to /admin over HTTP was {code}, not refused")
        check(
            docker("exec", name, "python", "-m", "brief.healthcheck", check=False).returncode == 0,
            "the Docker health check failed",
        )

        # ---- HTTPS side: nothing without a login, and no sign-in without CSRF
        code, headers, _ = anon.get(f"{https_url}/admin")
        check(code == 303 and "/admin/login" in headers["Location"], f"unauthenticated /admin was {code}")
        check("server" not in {k.lower() for k in headers.keys()}, "the Server header should be absent")
        check("default-src 'none'" in headers.get("Content-Security-Policy", ""), "the CSP header is missing")
        code, _, _ = anon.post(f"{https_url}/admin/login", {"username": "admin", "password": GOOD})
        check(code == 403, f"a sign-in without a CSRF token was {code}")

        # ---- first-run setup with the one-time token from the log
        logs = docker("logs", name, check=False)
        lines = [json.loads(x) for x in (logs.stdout + logs.stderr).splitlines() if x.startswith("{")]
        tokens = [x for x in lines if x.get("event") == "setup_token"]
        check(len(tokens) == 1, f"expected one setup_token log line, found {len(tokens)}")
        token = tokens[0]["token"] if tokens else "missing"
        code, _, page = anon.get(f"{https_url}/admin/setup")
        csrf = csrf_in(page)
        form = {"csrf_token": csrf, "token": token, "username": "admin", "password": GOOD, "confirm": GOOD}
        code, _, _ = anon.post(f"{https_url}/admin/setup", {**form, "token": "wrong"})
        check(code == 401, f"setup with a wrong token was {code}")
        code, _, _ = anon.post(f"{https_url}/admin/setup", form)
        check(code == 303, f"setup with the right token was {code}")
        code, _, _ = anon.post(f"{https_url}/admin/setup", form)
        check(code in (403, 404), f"the setup token worked twice ({code})")

        # ---- sign in: the cookie, the session, CSRF on writes
        me = Client()
        _, _, page = me.get(f"{https_url}/admin/login")
        code, headers, _ = me.post(f"{https_url}/admin/login", {"username": "admin", "password": GOOD,
                                                              "csrf_token": csrf_in(page), "next": "/admin"})  # fmt: skip
        cookie = headers.get("Set-Cookie", "").lower()
        check(code == 303 and "__host-brief_session=" in cookie, f"sign-in was {code}")
        check(
            "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie,
            f"cookie flags: {cookie}",
        )
        code, _, page = me.get(f"{https_url}/admin")
        check(code == 200 and "Overview" in page, f"/admin after sign-in was {code}")
        for path in (
            "/admin/sources", "/admin/findings", "/admin/audit", "/admin/overview/live", "/admin/config",
            "/admin/config/systems.yaml", "/admin/network", "/admin/systems", "/admin/backups",
            "/admin/signatures", "/admin/logs", "/admin/replay",
        ):  # fmt: skip
            code, headers, body = me.get(f"{https_url}{path}")
            check(code == 200, f"{path} was {code}")
            csp = headers.get("Content-Security-Policy", "")
            check("script-src 'self'" in csp and "unsafe" not in csp, f"{path} CSP: {csp}")
        for path in (
            "/static/admin.js",
            "/static/vendor/htmx-2.0.11.min.js",
            "/static/vendor/alpine-csp-3.17.4.min.js",
        ):
            check(anon.get(f"{https_url}{path}")[0] == 200, f"{path} is not served")
        session_csrf = csrf_in(me.get(f"{https_url}/admin/account")[2])
        code, _, _ = me.post(
            f"{https_url}/admin/account", {"current": GOOD, "password": CHANGED, "confirm": CHANGED}
        )
        check(code == 403, f"a write without the CSRF token was {code}")
        code, _, _ = me.post(f"{https_url}/admin/logout", {}, headers={"Origin": "https://evil.example"})
        check(code == 403, f"a cross-site sign-out was {code}")

        # ---- the CLI changes the password and ends the session
        cli = subprocess.run(["docker", "exec", "-i", name, "brief", "admin", "set-password", "--password-stdin"],
                             input=CHANGED + "\n", capture_output=True, text=True)  # fmt: skip
        check(
            cli.returncode == 0 and "changed" in cli.stdout,
            f"brief admin set-password: {cli.stdout} {cli.stderr}",
        )
        check(me.get(f"{https_url}/admin")[0] == 303, "the old session survived a password change")
        again = Client()
        _, _, page = again.get(f"{https_url}/admin/login")
        code, _, _ = again.post(f"{https_url}/admin/login", {"username": "admin", "password": CHANGED,
                                                           "csrf_token": csrf_in(page), "next": "/admin"})  # fmt: skip
        check(code == 303, f"signing in with the new password was {code}")
        code, _, _ = again.post(
            f"{https_url}/admin/logout", {"csrf_token": csrf_in(again.get(f"{https_url}/admin")[2])}
        )
        check(code == 303, f"signing out was {code}")
        del session_csrf

        # ---- throttling
        victim = Client()
        _, _, page = victim.get(f"{https_url}/admin/login")
        token = csrf_in(page)
        codes = [victim.post(f"{https_url}/admin/login", {"username": "admin", "password": "nope nope nope nope",
                                                          "csrf_token": token})[0] for _ in range(7)]  # fmt: skip
        check(codes[:5] == [401] * 5 and codes[-1] == 429, f"throttling: {codes}")
        code, headers, _ = victim.post(
            f"{https_url}/admin/login", {"username": "admin", "password": CHANGED, "csrf_token": token}
        )
        check(
            code == 429 and int(headers.get("Retry-After", 0)) > 0,
            f"the right password during a lock was {code}",
        )

        audit = json.loads(docker("exec", name, "python", "-c",
            "import json,sqlite3;c=sqlite3.connect('file:/state/baseline.db?mode=ro',uri=True);"
            "print(json.dumps([r[0] for r in c.execute('select action from audit_log')]))").stdout)  # fmt: skip
        for action in (
            "admin.created",
            "login.ok",
            "login.failed",
            "login.throttled",
            "password.changed",
            "logout",
        ):
            check(action in audit, f"audit log is missing {action}")
        print(f"[web] checks done; audit actions seen: {sorted(set(audit))}")
    except Exception as exc:  # report the logs and fail
        problems.append(f"{type(exc).__name__}: {exc}")
        print(f"[web] FAIL: {problems[-1]}")
    finally:
        if problems:
            log = docker("logs", name, check=False)
            print((log.stdout + log.stderr)[-1800:])
        docker("stop", "-t", "3", name, check=False)
        docker(
            "run",
            "--rm",
            "--entrypoint",
            "rm",
            "-v",
            f"{tmp}:/t",
            args.image,
            "-rf",
            "/t/state",
            "/t/archive",
            check=False,
        )
        subprocess.run(["rm", "-rf", str(tmp)], check=False)
    print("web e2e: PASS" if not problems else "web e2e: FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
