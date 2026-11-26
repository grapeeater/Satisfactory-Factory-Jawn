"""Outcome measurement from storage contents, separate from machine-state readback.

Machine state (IsPaused/IsProducing) says what a machine claims; storage deltas say what
the factory actually delivered. Both come from FRM in the live game, but from different
endpoints and different game objects, and the meter flags disagreement between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .snapshot import Snapshot


@dataclass
class OutcomeMeter:
    tracked_items: list[str]
    product_of: dict[str, str] = field(default_factory=dict)  # machine id -> item it outputs
    no_output_window_s: float = 60.0
    baseline: dict[str, dict[str, int]] | None = None  # storage -> item -> count at start
    produced: dict[str, int] = field(default_factory=dict)  # item -> delivered since start
    removed: dict[str, int] = field(default_factory=dict)  # item -> taken out externally
    _last: dict[str, dict[str, int]] = field(default_factory=dict)
    series: list[tuple[float, dict[str, int]]] = field(default_factory=list)
    reached_at: dict[str, float] = field(default_factory=dict)
    _producing_since: dict[str, tuple[float, int]] = field(default_factory=dict)
    _flagged: set[str] = field(default_factory=set)

    def update(self, snap: Snapshot, targets: dict[str, int]) -> list[dict[str, Any]]:
        anomalies: list[dict[str, Any]] = []
        if not snap.storages or snap.errors.get("getStorageInv"):
            return anomalies
        cur = {sid: dict(s.items) for sid, s in snap.storages.items()}
        if self.baseline is None:
            self.baseline = cur
            self._last = cur
            self.produced = {i: 0 for i in self.tracked_items}
            self.series.append((snap.taken_at, dict(self.produced)))
            return anomalies
        for sid, items in cur.items():
            prev = self._last.get(sid, {})
            for it in self.tracked_items:
                d = items.get(it, 0) - prev.get(it, 0)
                if d > 0:
                    self.produced[it] = self.produced.get(it, 0) + d
                elif d < 0:
                    self.removed[it] = self.removed.get(it, 0) - d
        self._last = cur
        self.series.append((snap.taken_at, dict(self.produced)))
        if len(self.series) > 5000:
            self.series = self.series[::2]
        for it, n in targets.items():
            if it not in self.reached_at and self.produced.get(it, 0) >= n:
                self.reached_at[it] = snap.taken_at

        # cross-check: a machine claiming to produce should move its product into storage
        for mid, m in snap.machines.items():
            it = self.product_of.get(mid)
            if it is None:
                continue
            if m.producing:
                since = self._producing_since.get(mid)
                if since is None:
                    self._producing_since[mid] = (snap.taken_at, self.produced.get(it, 0))
                elif snap.taken_at - since[0] > self.no_output_window_s and self.produced.get(it, 0) == since[1] and mid not in self._flagged:
                    self._flagged.add(mid)
                    anomalies.append({"machine": mid, "item": it, "kind": "producing_without_delivery", "window_s": self.no_output_window_s})
                elif self.produced.get(it, 0) != since[1]:
                    self._producing_since[mid] = (snap.taken_at, self.produced.get(it, 0))
                    self._flagged.discard(mid)
            else:
                self._producing_since.pop(mid, None)
        return anomalies

    def total(self, item: str) -> int:
        return self.produced.get(item, 0)
