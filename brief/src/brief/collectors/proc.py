"""Run a system tool (snmpget, ipmitool) for a collector, with errors that are safe to show.

The tools are Debian packages in the image. They are started without a shell, with a timeout and a cap on
how much output is read, and secrets are never put on the command line: the caller hands them over through
the environment or a throwaway config file. The error message names what went wrong, not the address or the
credentials; the tool's own words go to the (scrubbed) log only.
"""

import subprocess
from collections.abc import Mapping

MAX_OUTPUT = 2_000_000

# (words in the tool's message) -> what to tell the user
REASONS = (
    ("timeout", "no answer (timed out)"),
    ("no response", "no answer (timed out)"),
    ("authentication failure", "the credentials were rejected"),
    ("unknown user name", "the credentials were rejected"),
    ("wrong digest", "the credentials were rejected"),
    ("unauthorized name", "the credentials were rejected"),
    ("rakp 2 hmac is invalid", "the credentials were rejected"),
    ("invalid user name", "the credentials were rejected"),
    ("insufficient privilege", "the account has too little privilege"),
    ("connection refused", "the connection was refused"),
    ("unable to establish", "could not connect"),
    ("unknown host", "the address could not be resolved"),
    ("name or service not known", "the address could not be resolved"),
)


class ProcError(Exception):
    """`message` is safe to show; `detail` is the tool's own text, for the log only."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message, self.detail = message, detail


def explain(text: str, default: str) -> str:
    low = text.lower()
    for words, reason in REASONS:
        if words in low:
            return reason
    return default


def run(argv: list[str], env: Mapping[str, str] | None = None, timeout: float = 20.0) -> str:
    """Stdout of the command. Raises ProcError (never a bare subprocess exception)."""
    tool = argv[0]
    try:
        done = subprocess.run(  # noqa: S603 - a fixed argv list, no shell
            argv,
            env=dict(env) if env is not None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise ProcError(f"{tool} is not installed in this image") from None
    except subprocess.TimeoutExpired:
        raise ProcError("no answer (timed out)") from None
    except OSError as exc:
        raise ProcError(f"could not start {tool}", type(exc).__name__) from None
    out = done.stdout[:MAX_OUTPUT].decode("utf-8", "replace")
    err = done.stderr[:20000].decode("utf-8", "replace")
    if done.returncode != 0:
        raise ProcError(explain(err + out, f"{tool} reported an error"), (err or out).strip()[:300])
    return out
