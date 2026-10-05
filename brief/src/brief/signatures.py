"""Message signatures: Drain3 turns near-duplicate lines into one template.

Each message is mined as "<app>: <message>" so templates stay app-specific, and the masking rules
in drain3.ini (IPs, MACs, numbers) are what collapse thousands of lines into one signature.

Drain3 normally rewrites its whole state file (a jsonpickle dump of the template tree) on every new
cluster or template change. That dominated ingest time (about 88% in a profile of 150k real events,
and it grows with the number of clusters), so here it saves only when told to: `save()` is called
once per ingest cycle, just BEFORE the database commit. That keeps the invariant that matters: the
miner on disk is never older than the database. A crash before the save leaves both as they were;
a crash between the save and the commit leaves the miner ahead, which is harmless. (A miner that
lagged the database would hand a used cluster id to a different template.) The pipeline maps
Drain3's cluster ids to our own sig_id (see store.py).

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


class _DeferredMiner(TemplateMiner):
    """A TemplateMiner that remembers a save is due instead of serializing the tree on every change."""

    dirty = False

    def save_state(self, snapshot_reason):
        # add_log_message calls this for every new cluster or template change, and also on a timer
        if snapshot_reason != "periodic":
            self.dirty = True

    def write_state(self) -> bool:
        if not self.dirty:
            return False
        super().save_state("end of cycle")
        self.dirty = False
        return True


def _open(state_path: Path, config: TemplateMinerConfig) -> _DeferredMiner:
    return _DeferredMiner(FilePersistence(str(state_path)), config)


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

    def save(self) -> bool:
        """Write the miner state if anything changed since the last save. Call it before the commit."""
        return self.miner.write_state()

    @property
    def cluster_count(self) -> int:
        return len(self.miner.drain.clusters)
