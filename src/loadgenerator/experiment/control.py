"""Out-of-band workload accounting; values are never sent to the SUT."""

from collections import Counter
from gevent.lock import Semaphore


class WorkloadControl:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self._lock = Semaphore()
        self.accepting_flows = False
        self.measurement_enabled = False
        self._next_user = 0
        self._next_flow = 0
        self.active_flows = 0
        self.peak_active_users = 0
        self.flows_started = 0
        self.flows_completed = 0
        self.flows_failed = 0
        self.warmup_flows_started = 0
        self.warmup_flows_completed = 0
        self.warmup_flows_failed = 0
        self.failures_by_stage: Counter[str] = Counter()
        self.failed_flow_samples: list[dict] = []

    def allocate_user_id(self) -> str:
        with self._lock:
            self._next_user += 1
            return f"user-{self._next_user:04d}"

    def observe_users(self, count: int) -> None:
        with self._lock:
            self.peak_active_users = max(self.peak_active_users, count)

    def begin_flow(self, user_id: str) -> dict | None:
        with self._lock:
            if not self.accepting_flows:
                return None
            self._next_flow += 1
            phase = "workload" if self.measurement_enabled else "warmup"
            flow = {
                "flow_id": f"{self.run_id}-flow-{self._next_flow:06d}",
                "user_id": user_id,
                "phase": phase,
            }
            self.active_flows += 1
            if phase == "workload":
                self.flows_started += 1
            else:
                self.warmup_flows_started += 1
            return flow

    def end_flow(self, flow: dict, *, completed: bool, failure: dict | None = None) -> None:
        with self._lock:
            self.active_flows -= 1
            phase = flow["phase"]
            if completed:
                if phase == "workload":
                    self.flows_completed += 1
                else:
                    self.warmup_flows_completed += 1
                return

            if phase == "workload":
                self.flows_failed += 1
            else:
                self.warmup_flows_failed += 1
            failure = failure or {}
            stage = failure.get("stage", "unknown")
            self.failures_by_stage[stage] += 1
            if len(self.failed_flow_samples) < 100:
                self.failed_flow_samples.append({
                    "flow_id": flow["flow_id"],
                    "user_id": flow["user_id"],
                    "phase": phase,
                    "stage": stage,
                    "http_status": failure.get("http_status"),
                    "error": failure.get("error", "request failed"),
                })
