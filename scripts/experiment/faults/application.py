from __future__ import annotations

import shlex

from scripts.experiment.orchestrator.kube import deployment_status, ready_pods, run_kubectl, wait_until
from .base import FaultInjector


class ApplicationFaultInjector(FaultInjector):
    flag_path = "/tmp/tcc-faults/payment-charge-exception"

    def __init__(self):
        self.pods: list[dict] = []
        self.applied = False

    def prepare(self) -> dict:
        status = deployment_status("paymentservice")
        pods = [p for p in ready_pods("app=paymentservice") if p["ready"]]
        if not status["ready"] or len(pods) != status["ready_replicas"] or not pods:
            raise RuntimeError("paymentservice must be fully Ready before F4")
        self.pods = pods
        for pod in pods:
            result = run_kubectl("exec", "-n", "default", pod["name"], "-c", "server", "--", "sh", "-c", "test -d /tmp/tcc-faults && test -w /tmp/tcc-faults && test \"${TCC_FAULT_INJECTION_ENABLED}\" = true", check=False)
            if result.returncode:
                raise RuntimeError(f"F4 hook is not enabled or its control directory is not writable in {pod['name']}")
            stale = run_kubectl("exec", "-n", "default", pod["name"], "-c", "server", "--", "test", "!", "-e", self.flag_path, check=False)
            if stale.returncode:
                raise RuntimeError(f"stale F4 control flag exists in {pod['name']}")
        return {"pods": self.pods}

    def apply(self) -> dict:
        self.applied = True
        applied = []
        for pod in self.pods:
            command = f"umask 077; printf '%s' enabled > {shlex.quote(self.flag_path)}"
            run_kubectl("exec", "-n", "default", pod["name"], "-c", "server", "--", "sh", "-c", command)
            applied.append(pod["name"])
        return {"flag_path": self.flag_path, "pods": applied}

    def confirm_active(self, timeout: float) -> dict:
        return wait_until(lambda: self._flag_state(expected=True), timeout, "F4 fault flag to become present")

    def recover(self) -> dict:
        for pod in self.pods:
            run_kubectl("exec", "-n", "default", pod["name"], "-c", "server", "--", "rm", "-f", self.flag_path, check=False)
        self.applied = False
        return {"flag_removed": True}

    def confirm_recovered(self, timeout: float) -> dict:
        evidence = wait_until(lambda: self._flag_state(expected=False), timeout, "F4 fault flag to be removed")
        status = deployment_status("paymentservice")
        current = {p["uid"]: p for p in ready_pods("app=paymentservice") if p["ready"]}
        missing_or_restarted = [p["name"] for p in self.pods if p["uid"] not in current or current[p["uid"]]["restarts"] != p["restarts"]]
        if not status["ready"] or missing_or_restarted:
            raise RuntimeError(f"paymentservice did not remain Ready in original pods without restarts: {missing_or_restarted}")
        return {**evidence, "deployment": status, "pods": [current[p["uid"]] for p in self.pods]}

    def cleanup(self) -> dict:
        if self.pods:
            self.recover()
            return self.confirm_recovered(60)
        return {"recovered": True, "no_pods_prepared": True}

    def _flag_state(self, *, expected: bool) -> tuple[bool, dict]:
        checks = {}
        for pod in self.pods:
            result = run_kubectl("exec", "-n", "default", pod["name"], "-c", "server", "--", "test", "-e", self.flag_path, check=False)
            if result.returncode and result.stderr.strip():
                raise RuntimeError(f"could not inspect F4 control flag in {pod['name']}: {result.stderr.strip()}")
            checks[pod["name"]] = result.returncode == 0
        return all(value is expected for value in checks.values()) and bool(checks), {"flags_present_by_pod": checks}
