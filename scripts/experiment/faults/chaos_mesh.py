from __future__ import annotations

import json
import re

from scripts.experiment.orchestrator.kube import deployment_status, kubectl_json, run_kubectl, wait_until
from .base import FaultInjector


class ChaosMeshInjector(FaultInjector):
    def __init__(self, fault_class: str, run_id: str, parameters: dict):
        self.fault_class = fault_class
        self.parameters = parameters
        suffix = re.sub(r"[^a-z0-9-]", "-", run_id.lower())[-32:].strip("-")
        self.name = f"tcc-{fault_class.lower()}-{suffix}"[:63].rstrip("-")
        self.kind = "StressChaos" if fault_class == "F1" else "NetworkChaos"
        self.applied = False

    def prepare(self) -> dict:
        if run_kubectl("get", "crd", f"{self.kind.lower()}.chaos-mesh.org", check=False).returncode:
            raise RuntimeError(f"Chaos Mesh CRD for {self.kind} is not installed")
        controller = kubectl_json("get", "deployment", "chaos-controller-manager", "-n", "chaos-mesh")
        daemonset = kubectl_json("get", "daemonset", "chaos-daemon", "-n", "chaos-mesh")
        controller_status = controller.get("status", {})
        ds_status = daemonset.get("status", {})
        if int(controller_status.get("readyReplicas", 0)) < 1 or int(ds_status.get("numberReady", 0)) < int(ds_status.get("desiredNumberScheduled", 1)):
            raise RuntimeError("Chaos Mesh controller/daemon are not Ready")
        active = run_kubectl("get", "stresschaos,networkchaos", "-A", "-o", "json", check=False)
        if active.returncode == 0:
            items = json.loads(active.stdout).get("items", [])
            if any(not _condition(item, "AllRecovered") for item in items):
                raise RuntimeError("an existing Chaos Mesh experiment is not fully recovered")
        target = "checkoutservice" if self.fault_class == "F1" else "paymentservice"
        target_status = deployment_status(target)
        if not target_status["ready"]:
            raise RuntimeError(f"Chaos target {target} is not Ready")
        return {
            "chaos_mesh_controller_image": controller["spec"]["template"]["spec"]["containers"][0]["image"],
            "chaos_daemon_image": daemonset["spec"]["template"]["spec"]["containers"][0]["image"],
            "target": target,
        }

    def manifest(self) -> dict:
        selector = {"namespaces": ["default"], "labelSelectors": {"app": "checkoutservice" if self.fault_class == "F1" else "checkoutservice"}}
        spec: dict
        if self.fault_class == "F1":
            spec = {
                "mode": "all", "selector": selector,
                "stressors": {"cpu": {"workers": int(self.parameters["workers"]), "load": int(self.parameters["load_percent"])}},
                "duration": "1h",
            }
        else:
            spec = {
                "action": "delay", "mode": "all", "selector": selector,
                "direction": "to",
                "target": {
                    "mode": "all",
                    "selector": {"namespaces": ["default"], "labelSelectors": {"app": "paymentservice"}},
                },
                "delay": {
                    "latency": f"{int(self.parameters['latency_ms'])}ms",
                    "correlation": str(int(self.parameters.get("correlation_percent", 100))),
                    "jitter": f"{int(self.parameters.get('jitter_ms', 0))}ms",
                },
                "duration": "1h",
            }
        return {
            "apiVersion": "chaos-mesh.org/v1alpha1", "kind": self.kind,
            "metadata": {"name": self.name, "namespace": "default", "labels": {"app.kubernetes.io/part-of": "tcc-experiment"}},
            "spec": spec,
        }

    def apply(self) -> dict:
        manifest = self.manifest()
        run_kubectl("apply", "-f", "-", input_text=json.dumps(manifest))
        self.applied = True
        return {"resource": self.name, "kind": self.kind, "manifest": manifest}

    def confirm_active(self, timeout: float) -> dict:
        def active() -> tuple[bool, dict]:
            item = kubectl_json("get", self.kind.lower(), self.name, "-n", "default")
            injected = _condition(item, "AllInjected")
            selected = _condition(item, "Selected")
            return injected and selected, _chaos_evidence(item)
        evidence = wait_until(active, timeout, f"{self.name} to be selected and fully injected")
        if self.fault_class == "F1" and not deployment_status("checkoutservice")["ready"]:
            raise RuntimeError("checkoutservice became unready during CPU stress injection")
        return evidence

    def recover(self) -> dict:
        result = run_kubectl("delete", self.kind.lower(), self.name, "-n", "default", "--wait=true", "--timeout=120s", check=False)
        if result.returncode and "NotFound" not in result.stderr:
            raise RuntimeError(result.stderr.strip())
        self.applied = False
        return {"resource": self.name, "deleted": True}

    def confirm_recovered(self, timeout: float) -> dict:
        def recovered() -> tuple[bool, dict]:
            result = run_kubectl("get", self.kind.lower(), self.name, "-n", "default", "-o", "json", check=False)
            if result.returncode and "NotFound" in result.stderr:
                return True, {"resource_absent": True}
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            item = json.loads(result.stdout)
            return _condition(item, "AllRecovered"), _chaos_evidence(item)
        return wait_until(recovered, timeout, f"{self.name} to be recovered and removed")

    def cleanup(self) -> dict:
        result = run_kubectl("get", self.kind.lower(), self.name, "-n", "default", check=False)
        if self.applied or result.returncode == 0:
            self.recover()
            return self.confirm_recovered(150)
        return {"recovered": True, "resource_absent": True}


def _condition(item: dict, name: str) -> bool:
    return any(c.get("type") == name and str(c.get("status", "")).lower() == "true" for c in item.get("status", {}).get("conditions", []))


def _chaos_evidence(item: dict) -> dict:
    status = item.get("status", {})
    return {
        "name": item.get("metadata", {}).get("name"),
        "phase": status.get("experiment", {}).get("phase"),
        "conditions": status.get("conditions", []),
    }
