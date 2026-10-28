from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
import time


class EventLog:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._lock = Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **details) -> None:
        from .ground_truth import utc_now
        record = {
            "source": "orchestrator", "run_id": self.run_id, "event": event,
            "timestamp": utc_now(), "monotonic_seconds": time.monotonic(), **details,
        }
        encoded = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)

    def read_workload_events(self) -> list[dict]:
        if not self.path.exists():
            return []
        result = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("source") == "workload":
                result.append(event)
        return result
