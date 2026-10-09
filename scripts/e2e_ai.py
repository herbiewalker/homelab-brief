#!/usr/bin/env python3
"""End-to-end check of the AI settings, provider tests, and the admin AI page against the built image
(Linux or WSL, Docker).

Starts a tiny OpenAI-compatible server (scripts/fake_openai.py) and the app on one network, then checks:
  - AI is off as shipped (`brief ai status`) and the shipped prompts are in the image and editable by name
  - `brief ai provider ... model`, `brief ai test`, `brief ai models`, `brief ai calls` against the real HTTP transport,
    with the bearer key from a template variable (the fake server refuses any other)
  - a refused edit (a provider switched on with no real address; a chain without the template) changes nothing
  - the HTTPS admin page: sign in, see the providers with the key shown as "set" and never its value, run
    "Test connection" and "List models" in place (with CSRF), and save a prompt through the page
  - the call log records the tests without any content

  python3 scripts/e2e_ai.py [--image homelab-brief:dev] [--http-port 18084] [--https-port 18444]
"""

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_web  # noqa: E402  (its HTTPS client)

ROOT = Path(__file__).resolve().parent.parent
NET, FAKE, APP = "hb-e2e-ai-net", "hb-e2e-ai-fake", "hb-e2e-ai-app"
KEY = "e2e-ai-key"
PASSWORD = "correct horse battery staple"


def docker(*args, check=True, **kw):
    return e2e_web.docker(*args, check=check, **kw)


def brief(*args, check=False, **kw):
    return docker("exec", APP, "brief", *args, check=check, **kw)


def until(what, check, timeout=90):
    for _ in range(timeout):
        try:
            if check():
                return
        except Exception:  # not up yet
            pass
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {what}")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--image", default="homelab-brief:dev")
    ap.add_argument("--http-port", type=int, default=18084)
    ap.add_argument("--https-port", type=int, default=18444)
    args = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="hb-e2e-ai-"))
    for d in ("state", "archive", "config/brief"):
        (tmp / d).mkdir(parents=True)
        (tmp / d).chmod(0o777)
    (tmp / "config").chmod(0o777)
    problems: list[str] = []

    def check(ok, message):
        if not ok:
            problems.append(message)
            print(f"[ai] FAIL: {message}")

    try:
        docker("network", "rm", NET, check=False)
        docker("network", "create", NET)
        docker("rm", "-f", FAKE, APP, check=False)
        docker("run", "-d", "--name", FAKE, "--network", NET, "-v", f"{ROOT}/scripts/fake_openai.py:/fake.py:ro",
               "--entrypoint", "python", args.image, "/fake.py")  # fmt: skip
        docker("run", "-d", "--name", APP, "--network", NET, "-e", "SYSLOG_PORT=5514", "-e", "BRIEF_INGEST_INTERVAL=1",
               "-e", f"HTTPS_PUBLIC_PORT={args.https_port}", "-e", f"AI_LOCAL_API_KEY={KEY}",
               "-v", f"{tmp}/state:/state", "-v", f"{tmp}/config:/config", "-v", f"{tmp}/archive:/archive",
               "-p", f"{args.http_port}:80", "-p", f"{args.https_port}:443", args.image)  # fmt: skip
        until(
            "the app",
            lambda: docker("exec", APP, "python", "-m", "brief.healthcheck", check=False).returncode == 0,
        )

        status = brief("ai", "status")
        check(status.returncode == 0 and "AI is off" in status.stdout, f"status as shipped: {status.stdout}")
        shown = brief("config", "show", "prompts/morning_brief.md")
        check(
            shown.returncode == 0 and "\n---\n" in shown.stdout, "the shipped prompt is not available by name"
        )
        check(
            brief("config", "show", "prompts/../ai.yaml").returncode != 0,
            "a path outside the prompt list was accepted",
        )

        # a refused edit changes nothing
        refused = brief("ai", "provider", "local", "on")
        check(
            refused.returncode == 1 and "no real base_url" in refused.stdout + refused.stderr,
            f"refusal: {refused.stderr}",
        )
        before = brief("config", "show", "ai.yaml").stdout

        # point the shipped file at the fake server, through the config editor (as a person would)
        text = before.replace("http://<model-server-ip>:11434/v1", f"http://{FAKE}:8083/v1")
        text = text.replace("    enabled: false\n\n  - id: cloud", "    enabled: true\n\n  - id: cloud", 1)
        put = subprocess.run(["docker", "exec", "-i", APP, "brief", "config", "set", "ai.yaml"],
                             input=text, capture_output=True, text=True)  # fmt: skip
        check(put.returncode == 0, f"config set ai.yaml: {put.stdout} {put.stderr}")
        model = brief("ai", "provider", "local", "model", "e2e-model")
        check(model.returncode == 0, f"set model: {model.stdout} {model.stderr}")
        chain = brief("ai", "chain", "explain_finding", "local", "cloud", "template")
        check(chain.returncode == 0, f"chain: {chain.stderr}")
        bad_chain = brief("ai", "chain", "explain_finding", "local")
        check(
            bad_chain.returncode == 1 and "must end with template" in bad_chain.stdout + bad_chain.stderr,
            "a chain without the template was accepted",
        )

        models = brief("ai", "models", "local")
        check(models.stdout.split() == ["e2e-model", "e2e-other"], f"models: {models.stdout} {models.stderr}")
        test = brief("ai", "test", "local")
        check(
            test.returncode == 0 and "connection: ok" in test.stdout and "tool calling: works" in test.stdout,
            f"test: {test.stdout} {test.stderr}",
        )
        check("models listed: 2" in test.stdout, "the model list was not counted")
        calls = brief("ai", "calls").stdout
        check("provider_test" in calls and "ok" in calls, f"call log: {calls}")

        # the wrong key is refused by the server and reported, not hidden
        wrong = docker(
            "exec", "-e", "AI_LOCAL_API_KEY=wrong", APP, "brief", "ai", "test", "local", check=False
        )
        check(
            wrong.returncode == 1 and "FAILED" in wrong.stdout, f"a wrong key: {wrong.stdout} {wrong.stderr}"
        )

        # the admin page over HTTPS
        cli = subprocess.run(["docker", "exec", "-i", APP, "brief", "admin", "set-password", "--password-stdin"],
                             input=PASSWORD + "\n", capture_output=True, text=True)  # fmt: skip
        check(cli.returncode == 0, f"set-password: {cli.stderr}")
        https = f"https://127.0.0.1:{args.https_port}"
        client = e2e_web.Client()
        until("HTTPS", lambda: client.get(f"{https}/admin/login")[0] == 200)
        _, _, page = client.get(f"{https}/admin/login")
        code, _, _ = client.post(f"{https}/admin/login", {"username": "admin", "password": PASSWORD,
                                                        "csrf_token": e2e_web.csrf_in(page), "next": "/admin"})  # fmt: skip
        check(code == 303, f"sign-in was {code}")
        code, _, ai_page = client.get(f"{https}/admin/ai")
        check(
            code == 200 and "Master switch" in ai_page and "AI_LOCAL_API_KEY" in ai_page,
            f"the AI page: {code}",
        )
        check(KEY not in ai_page, "the API key value appeared on the AI page")
        token = e2e_web.csrf_in(ai_page)
        code, _, no_csrf = client.post(f"{https}/admin/ai/test/local", {})
        check(code == 403, f"a test without CSRF was {code}")
        code, _, result = client.post(f"{https}/admin/ai/test/local", {"csrf_token": token})
        check(
            code == 200 and "The connection works." in result,
            f"Test connection in the page: {code} {result[:300]}",
        )
        code, _, models_html = client.post(f"{https}/admin/ai/models/local", {"csrf_token": token})
        check("e2e-other" in models_html, "List models in the page")
        code, _, prompt = client.get(f"{https}/admin/ai/prompt/explain_finding")
        check(code == 200 and "Prompt: explain finding" in prompt, f"the prompt page: {code}")
        base = __import__("re").search(r'name="base" value="([^"]+)"', prompt).group(1)
        body = "Notes.\n---\nYou explain one finding briefly.\n"
        code, _, _ = client.post(
            f"{https}/admin/ai/prompt/explain_finding", {"csrf_token": token, "base": base, "text": body}
        )
        check(code == 303, f"saving a prompt was {code}")
        shown = brief("config", "show", "prompts/explain_finding.md")
        check(
            "You explain one finding briefly." in shown.stdout,
            "the saved prompt is not what the pipeline would read",
        )
        _, _, after = client.get(f"{https}/admin/ai")
        check(
            "provider_test" in after and "e2e-model" not in after.split("Call log")[-1],
            "the call log shows tests without content",
        )
        seen = docker("logs", FAKE, check=False)
        lines = [x for x in (seen.stdout + seen.stderr).splitlines() if x.startswith(("GET", "POST"))]
        good = [x for x in lines if x.endswith("auth_ok=True")]
        check(
            len(good) >= 6 and any("auth_ok=False" in x for x in lines),
            f"requests seen by the model server: {lines[-4:]}",
        )
        print(f"[ai] {len(lines)} requests reached the model server; {len(good)} carried the right key")
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {exc}")
        print(f"[ai] FAIL: {problems[-1]}")
    finally:
        if problems:
            print(docker("logs", APP, check=False).stdout[-1500:])
        docker("rm", "-f", FAKE, APP, check=False)
        docker("network", "rm", NET, check=False)
        docker("run", "--rm", "--entrypoint", "rm", "-v", f"{tmp}:/t", args.image, "-rf",
               "/t/state", "/t/archive", "/t/config", check=False)  # fmt: skip
        try:
            tmp.rmdir()
        except OSError:
            pass
    print("[ai] " + ("PASS" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
