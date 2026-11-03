"""Track changes in the distribution of recent prediction inputs."""

import json
from bisect import bisect_right
from collections import deque
from pathlib import Path
from threading import Lock


class DriftMonitor:
    def __init__(self, reference_path, window_size=500, min_samples=100):
        if not 1 <= min_samples <= window_size:
            raise ValueError("Invalid drift window or minimum sample count.")

        reference = json.loads(Path(reference_path).read_text())
        if reference["schema_version"] != 1 or reference["reference_split"] != "train":
            raise ValueError("Expected a version 1 training reference.")

        types = reference["transaction_type_counts"]
        self._type_index = {kind: i for i, kind in enumerate(types)}
        self._fields = ("type", "amount", "oldbalanceOrg")
        self._cuts = {
            name: reference["numeric"][name]["cut_points"]
            for name in self._fields[1:]
        }
        counts = {
            "type": list(types.values()),
            **{
                name: reference["numeric"][name]["counts"]
                for name in self._fields[1:]
            },
        }
        rows = reference["train_rows"]
        for name, values in counts.items():
            if rows <= 0 or sum(values) != rows or any(v < 0 for v in values):
                raise ValueError(f"Invalid reference counts: {name}")
            if name in self._cuts and len(values) != len(self._cuts[name]) + 1:
                raise ValueError(f"Invalid bucket count: {name}")

        self._reference = {
            name: [value / rows for value in values]
            for name, values in counts.items()
        }
        self._counts = {name: [0] * len(values) for name, values in counts.items()}
        self._window = deque()
        self._window_size = window_size
        self._min_samples = min_samples
        self._lock = Lock()

    def observe(self, kind, amount, balance):
        """Record one already validated, successful prediction."""
        buckets = (
            self._type_index[kind],
            bisect_right(self._cuts["amount"], amount),
            bisect_right(self._cuts["oldbalanceOrg"], balance),
        )
        with self._lock:
            if len(self._window) == self._window_size:
                for name, bucket in zip(self._fields, self._window.popleft()):
                    self._counts[name][bucket] -= 1
            self._window.append(buckets)
            for name, bucket in zip(self._fields, buckets):
                self._counts[name][bucket] += 1

    def snapshot(self):
        with self._lock:
            size = len(self._window)
            if size < self._min_samples:
                return size, {}

            scores = {
                name: 0.5 * sum(
                    abs(count / size - reference)
                    for count, reference in zip(
                        self._counts[name], self._reference[name]
                    )
                )
                for name in self._fields
            }
            return size, scores
