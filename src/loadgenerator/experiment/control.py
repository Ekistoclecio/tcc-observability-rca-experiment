"""Out-of-band open-loop workload accounting; values never reach the SUT."""

from gevent.lock import Semaphore


class WorkloadControl:
    def __init__(self, run_id: str, max_in_flight: int, target_rate: float):
        self.run_id = run_id
        self.max_in_flight = max_in_flight
        self._target_rate = target_rate
        self._lock = Semaphore()
        self._next_flow = 0
        self.active_flows = 0
        self.peak_in_flight = 0
        self.peak_in_flight_by_phase = {"warmup": 0, "steady": 0}
        self.phases = {name: self._empty_phase() for name in ("warmup", "steady")}
        self.failures_by_stage: dict[str, int] = {}
        self.dropped_by_reason = {name: {} for name in ("warmup", "steady")}
        self.failed_flow_samples: list[dict] = []
        self.start_lags_by_phase: dict[str, list[float]] = {"warmup": [], "steady": []}

    @staticmethod
    def _empty_phase() -> dict[str, int]:
        return {"scheduled": 0, "started": 0, "completed": 0, "failed": 0, "control_plane_dropped": 0}

    def schedule(self, phase: str, scheduled_at: float, actual_at: float) -> tuple[dict | None, str | None]:
        """Account for one arrival slot; never queue when capacity is exhausted."""
        with self._lock:
            counts = self.phases[phase]
            counts["scheduled"] += 1
            if actual_at - scheduled_at >= 1 / self._target_rate:
                counts["control_plane_dropped"] += 1
                reasons = self.dropped_by_reason[phase]
                reasons["scheduler_late"] = reasons.get("scheduler_late", 0) + 1
                return None, "scheduler_late"
            if self.active_flows >= self.max_in_flight:
                counts["control_plane_dropped"] += 1
                reasons = self.dropped_by_reason[phase]
                reasons["max_in_flight"] = reasons.get("max_in_flight", 0) + 1
                return None, "max_in_flight"
            self._next_flow += 1
            self.active_flows += 1
            self.peak_in_flight = max(self.peak_in_flight, self.active_flows)
            self.peak_in_flight_by_phase[phase] = max(
                self.peak_in_flight_by_phase[phase], self.active_flows
            )
            counts["started"] += 1
            self.start_lags_by_phase[phase].append(max(0.0, (actual_at - scheduled_at) * 1000))
            return {
                "flow_id": f"{self.run_id}-flow-{self._next_flow:06d}",
                "phase": phase,
                "scheduled_at_monotonic": scheduled_at,
                "actual_started_monotonic": actual_at,
            }, None

    def end_flow(self, flow: dict, *, completed: bool, failure: dict | None = None) -> None:
        with self._lock:
            self.active_flows -= 1
            counts = self.phases[flow["phase"]]
            counts["completed" if completed else "failed"] += 1
            if completed:
                return
            failure = failure or {}
            stage = failure.get("stage", "unknown")
            self.failures_by_stage[stage] = self.failures_by_stage.get(stage, 0) + 1
            if len(self.failed_flow_samples) < 100:
                self.failed_flow_samples.append({
                    "flow_id": flow["flow_id"],
                    "phase": flow["phase"],
                    "stage": stage,
                    "http_status": failure.get("http_status"),
                    "error": failure.get("error", "request failed"),
                })

    def snapshot(self, phase: str) -> dict[str, int]:
        with self._lock:
            return dict(self.phases[phase])
