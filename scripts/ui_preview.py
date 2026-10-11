"""Serve the admin and public pages over plain HTTP on synthetic data, to look at the interface in a browser.

Dev only: runs inside the test image (it imports the test helpers), seeds the golden-day sample data and a
throwaway admin account, and prints where it listens. Nothing here is real data.

    docker run --rm -e PYTHONPATH=/app/src -p 8099:8099 -v "$PWD/brief/src:/app/src" \
        -v "$PWD/scripts:/app/scripts" homelab-brief:test python /app/scripts/ui_preview.py

Sign in as admin / preview-password-123. PREVIEW_FAIL=1 shows the failed-run state.
"""

import json
import os
import random
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
for cand in (HERE.parent / "brief" / "tests", HERE.parent / "tests"):  # repo checkout, or the image layout
    if cand.is_dir():
        sys.path.insert(0, str(cand))

import golden_day as g  # noqa: E402
import uvicorn  # noqa: E402

from brief import health  # noqa: E402
from brief import web as w  # noqa: E402
from brief.auth import Auth  # noqa: E402
from brief.settings import Settings  # noqa: E402
from brief.store import Store, iso  # noqa: E402

PASSWORD = "preview-password-123"


def seed_history(store: Store, doc: dict) -> None:
    """Thirty days of system states, a day of event counts and memory samples, and a few alerts, so the
    trend lines, status bars, and timeline have something to draw. Synthetic and deterministic."""
    rng = random.Random(7)
    now = datetime.now(UTC)
    with store.write() as tx:
        for system in doc.get("systems") or []:
            for back in range(1, 30):
                day = f"{now - timedelta(days=back):%Y-%m-%d}"
                roll = rng.random()
                if roll < 0.04 and back % 11:
                    continue  # the container was off
                state = "red" if roll < 0.07 else "yellow" if roll < 0.18 else "green"
                tx.db.execute(
                    "INSERT OR REPLACE INTO system_day (day, system_id, worst, samples) VALUES (?, ?, ?, 24)",
                    (day, system["id"], state),
                )
        sig = tx.signature_for(1, 1, "app", "synthetic", now)
        for back in range(24):
            when = now - timedelta(hours=back)
            n = int(400 + 250 * rng.random() + (900 if 3 < back < 7 else 0))
            tx.add_hourly([(f"{when:%Y-%m-%d}", when.hour, "198.51.100.1", sig, "info", n)])
            tx.add_metrics(
                [
                    (when, "process.memory_mb", 420 + 40 * rng.random() + (24 - back) * 1.5),
                    (when, "storage.archive_bytes", (300 + (24 - back) * 0.8) * 1_000_000),
                ]
            )
        tx.add_push(
            "f-disk",
            "new",
            "alert",
            "warn",
            "Disk errors on the storage server",
            False,
            now - timedelta(hours=30),
        )
        tx.add_push(
            "f-disk",
            "escalated",
            "alert",
            "crit",
            "Disk errors on the storage server",
            False,
            now - timedelta(hours=18),
        )
        tx.add_push(
            "f-disk",
            "resolved",
            "alert",
            "warn",
            "Disk errors on the storage server",
            False,
            now - timedelta(hours=9),
        )
        tx.add_push(
            "f-silent",
            "new",
            "alert",
            "warn",
            "A host has been silent for 2 days",
            False,
            now - timedelta(hours=3),
        )


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="brief-preview-"))
    cfg = root / "cfg" / "brief"
    cfg.mkdir(parents=True)
    os.environ["BRIEF_CONFIG_DIR"] = str(root / "cfg")
    os.environ["BRIEF_DEFAULTS_DIR"] = str(root / "defaults")
    (root / "defaults" / "brief").mkdir(parents=True)
    (root / "defaults" / "brief" / "registry.yaml").write_bytes((g.BRIEF / "registry.yaml").read_bytes())
    store, _, out = g.build_daily(root / "daily")
    store.close()
    latest = root / "out" / "latest"
    latest.mkdir(parents=True)
    doc = json.loads((out / "latest" / "status.json").read_text())
    doc["generated_at"] = doc["daily_generated_at"] = iso(datetime.now(UTC))
    if os.environ.get("PREVIEW_FAIL"):
        doc["run_ok"] = False
    (latest / "status.json").write_text(json.dumps(doc))
    for name in ("brief.html", "board.html", "status.png"):
        if (out / "latest" / name).exists():
            (latest / name).write_bytes((out / "latest" / name).read_bytes())
    (root / "state").mkdir()
    (root / "archive").mkdir()
    health.receiver_ok = lambda port: True
    settings = Settings(
        state_dir=root / "state", announce_setup=False, out_dir=root / "out", archive_dir=root / "archive"
    )
    store = Store(root / "state" / "baseline.db")
    seed_history(store, doc)
    auth = Auth(store)
    auth.create_admin("admin", PASSWORD)
    app = w.create_app("https", settings, store, auth)
    print(f"preview on http://0.0.0.0:8099  (admin / {PASSWORD})", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8099, log_level="warning")


if __name__ == "__main__":
    main()
