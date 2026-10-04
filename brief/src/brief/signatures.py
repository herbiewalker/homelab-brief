"""Message signatures: Drain3 turns near-duplicate lines into one template.

Each message is mined as "<app>: <message>" so templates stay app-specific, and the masking rules
in drain3.ini (IPs, MACs, numbers) are what collapse thousands of lines into one signature. Drain3
saves its state to disk on every new cluster or template change, so the miner is always at least as
new as the database; the pipeline maps Drain3's cluster ids to our own sig_id (see store.py).

An exact-match cache in front of the miner makes a storm of identical lines cheap.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig

MAX_MINED_CHARS = 1000  # very long lines (JSON blobs) cost more than they tell us


@dataclass(frozen=True)
class Cluster:
    cluster_id: int
    template: str


def _open(state_path: Path, config: TemplateMinerConfig) -> TemplateMiner:
    return TemplateMiner(FilePersistence(str(state_path)), config)


class Signatures:
    def __init__(self, state_path: str | Path, ini_path: str | Path | None = None, cache_size: int = 50_000):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = TemplateMinerConfig()
        if ini_path and Path(ini_path).exists():
            self.config.load(str(ini_path))
        self.state_existed = self.state_path.exists()
        self.recovered = False
        try:
            self.miner = _open(self.state_path, self.config)
        except Exception:
            # An unreadable state file is treated as lost: set it aside and start a new miner. The
            # pipeline then starts a new generation, so old signatures cannot collide with new ones.
            self.state_path.rename(
                self.state_path.with_name(f"{self.state_path.name}.corrupt-{int(time.time())}")
            )
            self.state_existed, self.recovered = False, True
            self.miner = _open(self.state_path, self.config)
        self._cache: OrderedDict[tuple[str, str], Cluster] = OrderedDict()
        self._cache_size = cache_size

    def classify(self, app: str, msg: str) -> Cluster:
        key = (app, msg)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        label = app or "(none)"
        result = self.miner.add_log_message(f"{label}: {msg[:MAX_MINED_CHARS]}")
        template = str(result["template_mined"]).removeprefix(f"{label}: ")
        cluster = Cluster(int(result["cluster_id"]), template)
        self._cache[key] = cluster
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return cluster

    @property
    def cluster_count(self) -> int:
        return len(self.miner.drain.clusters)
