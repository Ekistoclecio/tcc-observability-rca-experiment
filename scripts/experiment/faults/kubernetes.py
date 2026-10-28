from __future__ import annotations

from scripts.experiment.orchestrator.kube import (
    deployment_status, endpoint_count, run_kubectl, wait_until,
)
from .base import FaultInjector


class KubernetesScaleInjector(FaultInjector):
    def __init__(self, deployment: str):
        self.deployment = deployment
        self.original_replicas: int | None = None
        self.applied = False

    def prepare(self) -> dict:
        status = deployment_status(self.deployment)
        if not status["ready"]:
            raise RuntimeError(f"target {self.deployment} is not at baseline Ready: {status}")
        self.original_replicas = status["desired_replicas"]
        if self.original_replicas < 1:
            raise RuntimeError(f"target {self.deployment} has no baseline replicas")
        return {"deployment": self.deployment, "original_replicas": self.original_replicas}

    def apply(self) -> dict:
        if self.original_replicas is None:
            raise RuntimeError("prepare must run before apply")
        run_kubectl("scale", f"deployment/{self.deployment}", "--replicas=0", "-n", "default")
        self.applied = True
        return {"requested_replicas": 0}

    def confirm_active(self, timeout: float) -> dict:
        return wait_until(
            lambda: _scaled_down(self.deployment), timeout,
            f"{self.deployment} Ready replicas and service endpoints to reach zero",
        )

    def recover(self) -> dict:
        if self.original_replicas is None:
            raise RuntimeError("original replica count was not captured")
        run_kubectl(
            "scale", f"deployment/{self.deployment}",
            f"--replicas={self.original_replicas}", "-n", "default",
        )
        self.applied = False
        return {"requested_replicas": self.original_replicas}

    def confirm_recovered(self, timeout: float) -> dict:
        return wait_until(
            lambda: _scaled_up(self.deployment, self.original_replicas or 0),
            timeout, f"{self.deployment} to restore its original Ready replicas",
        )

    def cleanup(self) -> dict:
        if self.original_replicas is not None:
            self.recover()
            evidence = self.confirm_recovered(180)
            return {"recovered": True, **evidence}
        return {"recovered": not self.applied, "original_replicas_captured": False}


def _scaled_down(deployment: str) -> tuple[bool, dict]:
    status = deployment_status(deployment)
    endpoints = endpoint_count(deployment)
    evidence = {**status, "service_endpoints": endpoints}
    return status["ready_replicas"] == 0 and endpoints == 0, evidence


def _scaled_up(deployment: str, replicas: int) -> tuple[bool, dict]:
    status = deployment_status(deployment)
    endpoints = endpoint_count(deployment)
    evidence = {**status, "service_endpoints": endpoints}
    return status["desired_replicas"] == replicas and status["ready_replicas"] == replicas and endpoints > 0, evidence
