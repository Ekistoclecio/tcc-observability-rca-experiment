#!/usr/bin/env python3
"""Run the frozen F1-F5 validation and publish artifacts only after all gates pass."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

from collect_raw import (
    CollectionError as RawCollectionError,
    query_url,
    readiness,
    request_bytes,
    reserve_local_port,
    trace_chunk_paths,
    wait_for_forward,
)
from scripts.experiment.fault_smoke import verify_checkout_network_effect_absent


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = ROOT / "docs/experiment"
VERSIONS = ROOT / "config/experiment/environment/versions.env"
FAULT_CONFIG = ROOT / "config/experiment/faults.json"
WORKLOAD = ROOT / "scripts/experiment/workload.py"
FAULT_RUNNER = ROOT / "scripts/experiment/fault_smoke.py"
COLLECTOR = ROOT / "scripts/experiment/collect_raw.py"
BUILDER = ROOT / "scripts/experiment/build_dataset.py"
RCA_BUILDER = ROOT / "scripts/experiment/build_rca_input.py"
SEMANTIC_VALIDATOR = ROOT / "scripts/experiment/validate_raw_semantics.py"
PROVENANCE_VALIDATOR = ROOT / "scripts/experiment/deployment/verify_provenance.py"

FAULTS = {
    "F1": ("cpu_saturation", "checkoutservice", "service"),
    "F2": ("network_latency", "checkoutservice -> paymentservice", "service_edge"),
    "F3": ("service_unavailable", "paymentservice", "service"),
    "F4": ("application_exception", "paymentservice/Charge", "operation"),
    "F5": ("datastore_unavailable", "redis-cart", "datastore"),
}
PROMETHEUS_CADVISOR_SCRAPE_SECONDS = 5.0
# A 15-second confirmed fault window spans at least three configured 5-second
# cAdvisor scrape periods, allowing temporal counter processing to observe a
# real delta without changing the fault mechanism.
MIN_FAULT_ACTIVE_SECONDS = 15.0


class IntegratedRunError(RuntimeError):
    pass


LAST_WORK_DIR: Path | None = None


class PreserveWorkOnError:
    """Keep intermediate evidence under /tmp when an integrated gate fails."""

    def __enter__(self) -> Path:
        global LAST_WORK_DIR
        self.path = Path(tempfile.mkdtemp(prefix="tcc-experiment-v2-battery-"))
        LAST_WORK_DIR = self.path
        return self.path

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is None:
            shutil.rmtree(self.path)
        return False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def datetime_from_epoch_ns(value: int) -> datetime:
    seconds, nanos = divmod(value, 1_000_000_000)
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=seconds, microseconds=nanos // 1_000
    )


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise IntegratedRunError(f"timestamp has no timezone: {value}")
    return parsed.astimezone(timezone.utc)


def run(args: list[str], *, env: dict[str, str] | None = None,
        cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise IntegratedRunError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout: {result.stdout[-4000:]}\nstderr: {result.stderr[-4000:]}"
        )
    return result


def json_output(result: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IntegratedRunError(f"{label} did not return JSON: {result.stdout[-2000:]}") from exc
    if not isinstance(value, dict):
        raise IntegratedRunError(f"{label} JSON must be an object")
    return value


def git_state() -> tuple[str, str, str]:
    branch = run(["git", "branch", "--show-current"]).stdout.strip()
    commit = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    status = run(["git", "status", "--porcelain"]).stdout
    return branch, commit, status


def load_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def run_script(path: Path, *arguments: str, output_path: Path | None = None,
               env: dict[str, str] | None = None) -> dict[str, Any]:
    result = run([sys.executable, str(path), *arguments], env=env)
    value = json_output(result, path.name)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return value


def check_no_residual_fault_state(kubectl: str, context: str, namespace: str) -> dict[str, Any]:
    chaos = run([kubectl, "--context", context, "get", "stresschaos,networkchaos",
                 "--all-namespaces", "-o", "json"], check=False)
    if chaos.returncode:
        raise IntegratedRunError(f"cannot inspect Chaos Mesh state: {chaos.stderr.strip()}")
    if json.loads(chaos.stdout).get("items"):
        raise IntegratedRunError("residual StressChaos/NetworkChaos resource exists")
    pod_network = run([kubectl, "--context", context, "get", "podnetworkchaos",
                       "--all-namespaces", "-o", "json"], check=False)
    if pod_network.returncode:
        raise IntegratedRunError(f"cannot inspect PodNetworkChaos state: {pod_network.stderr.strip()}")
    pod_network_items = json.loads(pod_network.stdout).get("items", [])
    nonempty_pod_network = [
        f"{item.get('metadata', {}).get('namespace')}/{item.get('metadata', {}).get('name')}"
        for item in pod_network_items if item.get("spec")
    ]
    if nonempty_pod_network:
        raise IntegratedRunError(
            f"PodNetworkChaos resources contain active configuration: {nonempty_pod_network}"
        )
    try:
        network_effect = verify_checkout_network_effect_absent(kubectl, context)
    except Exception as exc:
        raise IntegratedRunError(f"cannot prove checkout network fault cleanup: {exc}") from exc
    pods = json_output(run([kubectl, "--context", context, "-n", namespace,
                            "get", "pods", "-l", "app=paymentservice", "-o", "json"]),
                       "paymentservice Pods").get("items", [])
    if not pods:
        raise IntegratedRunError("paymentservice has no Pods for F4-hook preflight")
    for pod in pods:
        name = pod["metadata"]["name"]
        result = run([kubectl, "--context", context, "-n", namespace, "exec", name,
                      "-c", "server", "--", "sh", "-c",
                      'test ! -e /tmp/tcc-faults/payment-charge-exception'], check=False)
        if result.returncode:
            raise IntegratedRunError(f"F4 flag is present or unreadable in {name}")
    return {
        "stresschaos_networkchaos_count": 0,
        "podnetworkchaos_count": len(pod_network_items),
        "empty_podnetworkchaos_objects": nonempty_pod_network == [],
        "checkout_network_effect": network_effect,
        "f4_flags_absent": True,
    }


def check_jaeger_persistence(kubectl: str, context: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for side in ("a", "b"):
        namespace = f"observability-{side}"
        name = f"jaeger-{side}"
        deployment = json_output(run([
            kubectl, "--context", context, "-n", namespace,
            "get", "deployment", name, "-o", "json",
        ]), f"{name} deployment")
        template = deployment["spec"]["template"]["spec"]
        container = next((item for item in template.get("containers", [])
                          if item.get("name") == "jaeger"), None)
        if container is None:
            raise IntegratedRunError(f"{name} container is missing")
        env = {item["name"]: item.get("value") for item in container.get("env", [])}
        args = container.get("args", [])
        claim = f"jaeger-{side}-traces"
        if env.get("SPAN_STORAGE_TYPE") != "badger":
            raise IntegratedRunError(f"{name} is not configured for Badger storage")
        required_args = {
            "--badger.ephemeral=false",
            "--badger.directory-value=/badger/data",
            "--badger.directory-key=/badger/key",
            "--badger.span-store-ttl=168h0m0s",
            "--badger.consistency=true",
        }
        if not required_args.issubset(set(args)):
            raise IntegratedRunError(f"{name} Badger args differ from the frozen config: {args}")
        if not any(volume.get("name") == "trace-storage"
                   and volume.get("persistentVolumeClaim", {}).get("claimName") == claim
                   for volume in template.get("volumes", [])):
            raise IntegratedRunError(f"{name} does not use its dedicated PVC {claim}")
        if not any(mount.get("name") == "trace-storage" and mount.get("mountPath") == "/badger"
                   for mount in container.get("volumeMounts", [])):
            raise IntegratedRunError(f"{name} Badger directory is not mounted persistently")
        memory = container.get("resources", {}).get("limits", {}).get("memory")
        if memory != "512Mi":
            raise IntegratedRunError(f"{name} memory limit is unexpected: {memory}")
        pvc = json_output(run([
            kubectl, "--context", context, "-n", namespace,
            "get", "pvc", claim, "-o", "json",
        ]), f"{claim} PVC")
        if pvc.get("status", {}).get("phase") != "Bound":
            raise IntegratedRunError(f"{claim} is not Bound: {pvc.get('status', {}).get('phase')}")
        requested = pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")
        if requested != "1Gi":
            raise IntegratedRunError(f"{claim} has unexpected capacity request: {requested}")
        pod_status = json_output(run([
            kubectl, "--context", context, "-n", namespace,
            "get", "pods", "-l", f"app={name}", "-o", "json",
        ]), f"{name} pods")
        ready_pods = [pod for pod in pod_status.get("items", [])
                      if pod.get("status", {}).get("phase") == "Running"
                      and all(status.get("ready") for status in
                              pod.get("status", {}).get("containerStatuses", []))]
        if len(ready_pods) != 1:
            raise IntegratedRunError(f"{name} does not have exactly one Ready Pod")
        result[side.upper()] = {
            "storage": "badger",
            "pvc": claim,
            "pvc_phase": "Bound",
            "capacity": requested,
            "memory_limit": memory,
            "pod": ready_pods[0]["metadata"]["name"],
            "restart_count": ready_pods[0]["status"]["containerStatuses"][0].get("restartCount", 0),
        }
    return result


def preflight(output: Path, branch: str, commit: str, envvars: dict[str, str]) -> dict[str, Any]:
    if branch != "experiment-v2":
        raise IntegratedRunError(f"expected branch experiment-v2, got {branch!r}")
    if not commit or len(commit) != 40:
        raise IntegratedRunError("HEAD is not a full Git revision")
    _, _, status = git_state()
    if status.strip():
        raise IntegratedRunError(f"working tree is dirty:\n{status}")
    for destination in (EXPERIMENT / "results", EXPERIMENT / "teste",
                       EXPERIMENT / "stages/12-final-experiment-validation.md",
                       EXPERIMENT / "stages/12-final-experiment-validation.json"):
        if destination.exists():
            raise IntegratedRunError(
                f"final artifact destination already exists; refusing overwrite: {destination}"
            )
    env = os.environ.copy()
    env.update(envvars)
    checks = {}
    output.mkdir(parents=True, exist_ok=True)
    for name, script in (
        ("environment", ROOT / "scripts/experiment/environment/verify.sh"),
        ("scenario_a", ROOT / "scripts/experiment/observability-a/verify.sh"),
        ("scenario_b", ROOT / "scripts/experiment/observability-b/verify.sh"),
    ):
        result = run(["bash", str(script)], env=env)
        checks[name] = {"status": "PASS", "stdout": result.stdout[-4000:]}
    provenance_path = output / "deployment-provenance.json"
    provenance = run_script(PROVENANCE_VALIDATOR, "--output", str(provenance_path), env=env)
    if provenance.get("status") != "PASS" or provenance.get("validated_service_count") != 10:
        raise IntegratedRunError(f"deployment provenance gate failed: {provenance}")
    checks["deployment_provenance"] = {"status": "PASS", "services": 10}
    cluster = envvars["KIND_CLUSTER_NAME"]
    context = f"kind-{cluster}"
    checks["jaeger_persistence"] = check_jaeger_persistence(
        env.get("KUBECTL_BIN", "kubectl"), context
    )
    checks["residual_fault_state"] = check_no_residual_fault_state(
        env.get("KUBECTL_BIN", "kubectl"), context, envvars["SUT_NAMESPACE"]
    )
    checks["residual_fault_state"]["status"] = "PASS"
    return {"branch": branch, "tested_commit": commit, "working_tree_clean": True,
            "checks": checks, "deployment_provenance": provenance}


def run_normal_workload_with_forward(envvars: dict[str, str], env: dict[str, str],
                                     output_path: Path | None,
                                     workload_arguments: tuple[str, ...] = ()) -> dict[str, Any]:
    port = 18080
    process = subprocess.Popen(
        [env.get("KUBECTL_BIN", "kubectl"), "--context", f"kind-{envvars['KIND_CLUSTER_NAME']}",
         "-n", envvars["SUT_NAMESPACE"], "port-forward", "service/frontend", f"{port}:80"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = process.stdout.read() if process.stdout else ""
                raise IntegratedRunError(f"normal workload port-forward exited: {detail}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/_healthz", timeout=1):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        else:
            raise IntegratedRunError("normal workload port-forward readiness timed out")
        summary = run_script(WORKLOAD, "--base-url", f"http://127.0.0.1:{port}",
                             *workload_arguments, output_path=output_path, env=env)
        return summary
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()


def start_prometheus_forward(envvars: dict[str, str], env: dict[str, str], side: str
                             ) -> tuple[subprocess.Popen[str], Any, int]:
    namespace = f"observability-{side.lower()}"
    service = f"prometheus-{side.lower()}"
    remote_port = 9090
    local_port = reserve_local_port()
    output = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    process = subprocess.Popen(
        [env.get("KUBECTL_BIN", "kubectl"), "--context",
         f"kind-{envvars['KIND_CLUSTER_NAME']}", "-n", namespace, "port-forward",
         f"service/{service}", f"{local_port}:{remote_port}"],
        cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_for_forward(process, output, local_port, remote_port, time.monotonic() + 20)
    except RawCollectionError as exc:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        output.close()
        raise IntegratedRunError(f"Prometheus {side} port-forward failed: {exc}") from exc
    return process, output, local_port


def fresh_prometheus_instances(response: dict[str, Any], after_epoch: float,
                               identity_label: str) -> dict[str, float]:
    if response.get("status") != "success":
        raise IntegratedRunError(f"Prometheus instant query failed: {response}")
    fresh: dict[str, float] = {}
    for series in response.get("data", {}).get("result", []):
        labels = series.get("metric", {})
        identity = labels.get(identity_label)
        value = series.get("value")
        if not identity or not isinstance(value, list) or len(value) != 2:
            continue
        try:
            sample_timestamp = float(value[1])
        except (TypeError, ValueError):
            continue
        if sample_timestamp > after_epoch:
            fresh[str(identity)] = max(fresh.get(str(identity), 0.0), sample_timestamp)
    return fresh


def query_prometheus_instant(port: int, expression: str) -> dict[str, Any]:
    url = query_url(f"http://127.0.0.1:{port}", "/api/v1/query", {"query": expression})
    try:
        status, body = request_bytes(url, timeout=3)
        if status != 200:
            raise IntegratedRunError(f"HTTP {status} from Prometheus instant query")
        return json.loads(body)
    except (RawCollectionError, json.JSONDecodeError) as exc:
        raise IntegratedRunError(f"Prometheus instant query failed: {exc}") from exc


def duration_baseline_expressions() -> dict[str, dict[str, str]]:
    return {
        "A": {
            "count": 'timestamp(tcc_conventional_request_duration_seconds_count{service_name="checkoutservice",operation="/hipstershop.CheckoutService/PlaceOrder",status_code="OK"})',
            "sum": 'timestamp(tcc_conventional_request_duration_seconds_sum{service_name="checkoutservice",operation="/hipstershop.CheckoutService/PlaceOrder",status_code="OK"})',
        },
        "B": {
            "count": 'timestamp(otel_rpc_server_call_duration_seconds_count{exported_job="checkoutservice",rpc_method="hipstershop.CheckoutService/PlaceOrder",rpc_response_status_code="OK"})',
            "sum": 'timestamp(otel_rpc_server_call_duration_seconds_sum{exported_job="checkoutservice",rpc_method="hipstershop.CheckoutService/PlaceOrder",rpc_response_status_code="OK"})',
        },
    }


def wait_for_duration_baseline(envvars: dict[str, str], env: dict[str, str],
                               warmup_finished_at: str, timeout: float = 90.0
                               ) -> dict[str, Any]:
    after_epoch = parse_time(warmup_finished_at).timestamp()
    forwards: list[tuple[subprocess.Popen[str], Any]] = []
    ports: dict[str, int] = {}
    try:
        for side in ("A", "B"):
            process, output, port = start_prometheus_forward(envvars, env, side)
            forwards.append((process, output))
            ports[side] = port
            try:
                readiness(f"http://127.0.0.1:{port}/-/ready", time.monotonic() + 20)
            except RawCollectionError as exc:
                raise IntegratedRunError(f"Prometheus {side} readiness failed: {exc}") from exc

        expressions = duration_baseline_expressions()
        deadline = time.monotonic() + timeout
        last_observation: dict[str, Any] = {}
        while time.monotonic() < deadline:
            observations: dict[str, Any] = {}
            ready = True
            for side in ("A", "B"):
                identity_label = "pod" if side == "A" else "exported_instance"
                observations[side] = {}
                for concept, expression in expressions[side].items():
                    response = query_prometheus_instant(ports[side], expression)
                    instances = fresh_prometheus_instances(response, after_epoch, identity_label)
                    observations[side][concept] = {
                        "query": expression,
                        "identity_label": identity_label,
                        "fresh_instances": instances,
                    }
                    if not instances:
                        ready = False
                count_ids = set(observations[side]["count"]["fresh_instances"])
                sum_ids = set(observations[side]["sum"]["fresh_instances"])
                common_ids = sorted(count_ids & sum_ids)
                observations[side]["common_instances"] = common_ids
                if not common_ids:
                    ready = False
            last_observation = observations
            if ready:
                return {
                    "status": "PASS",
                    "metric_boundary": "checkoutservice PlaceOrder server duration count and sum",
                    "warmup_finished_at": warmup_finished_at,
                    "sample_timestamp_must_be_after_epoch": after_epoch,
                    "ready_at": time_text(utc_now()),
                    "scenarios": observations,
                }
            time.sleep(1.0)
        raise IntegratedRunError(
            "Prometheus A/B did not expose fresh PlaceOrder duration count+sum samples "
            f"from one instance after warmup within {timeout:g}s: {last_observation}"
        )
    finally:
        for process, _ in forwards:
            process.terminate()
        for process, output in forwards:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            output.close()


def run_normal_baseline(envvars: dict[str, str], env: dict[str, str],
                        output_path: Path) -> dict[str, Any]:
    warmup = run_normal_workload_with_forward(
        envvars, env, None, ("--steady-flows", "0")
    )
    if warmup.get("status") != "PASS" or not warmup.get("warmup"):
        raise IntegratedRunError(f"normal workload warmup failed: {warmup}")
    baseline_readiness = wait_for_duration_baseline(
        envvars, env, warmup["warmup_finished_at"]
    )
    measured = run_normal_workload_with_forward(
        envvars, env, None, ("--warmup-flows", "0")
    )
    if measured.get("status") != "PASS" or not measured.get("steady"):
        raise IntegratedRunError(f"normal measured workload failed: {measured}")
    summary = {
        "status": "PASS",
        "base_url": measured["base_url"],
        "started_at": measured["steady_started_at"],
        "measurement_started_at": measured["steady_started_at"],
        "warmup_started_at": warmup["started_at"],
        "warmup_finished_at": warmup["warmup_finished_at"],
        "finished_at": measured["finished_at"],
        "configuration": {
            **measured["configuration"],
            "warmup_flows": warmup["configuration"]["warmup_flows"],
            "steady_flows": measured["configuration"]["steady_flows"],
        },
        "warmup": warmup["warmup"],
        "steady": measured["steady"],
        "successful_warmup_flows": warmup["successful_warmup_flows"],
        "successful_steady_flows": measured["successful_steady_flows"],
        "baseline_readiness": baseline_readiness,
    }
    output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def collect(raw: Path, start: str, end: str, record: Path, commit: str,
            envvars: dict[str, str], env: dict[str, str],
            start_ns: int | None = None, end_ns: int | None = None) -> dict[str, Any]:
    args = [sys.executable, str(COLLECTOR), "--output-dir", str(raw), "--start", start,
            "--end", end, "--context", f"kind-{envvars['KIND_CLUSTER_NAME']}",
            "--collector-commit", commit, "--application-image-source-fingerprint", commit,
            "--workload-record", str(record), "--step", "1s", "--limit", "5000",
            "--trace-limit", "10000", "--drain-seconds", "20"]
    if (start_ns is None) != (end_ns is None):
        raise IntegratedRunError("exact RAW bounds must include both start_ns and end_ns")
    if start_ns is not None and end_ns is not None:
        args.extend(["--start-ns", str(start_ns), "--end-ns", str(end_ns)])
    result = run(args, env=env)
    report = json_output(result, "RAW collector")
    trace_query_diagnostics = []
    for line in result.stderr.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") in {
            "raw_backend_query", "TRACE_QUERY_START", "TRACE_QUERY_COMPLETE", "TRACE_QUERY_FAILED",
            "LOKI_QUERY_START", "LOKI_QUERY_COMPLETE", "LOKI_QUERY_SATURATED", "LOKI_QUERY_FAILED",
        }:
            trace_query_diagnostics.append(event)
    if report.get("status") != "PASS":
        raise IntegratedRunError(f"RAW collection failed: {report}")
    verify = run([sys.executable, str(COLLECTOR), "--verify-existing", str(raw)], env=env)
    verification = json_output(verify, "RAW integrity verifier")
    if verification.get("status") != "PASS":
        raise IntegratedRunError(f"RAW integrity failed: {verification}")
    return {
        "collection": report,
        "integrity": verification,
        "query_diagnostics": trace_query_diagnostics,
    }


def build_dataset(raw: Path, destination: Path, env: dict[str, str]) -> dict[str, Any]:
    result = run([sys.executable, str(BUILDER), "--raw", str(raw), "--output-dir", str(destination)], env=env)
    report = json_output(result, "Dataset Builder")
    if report.get("status") != "PASS":
        raise IntegratedRunError(f"Dataset Builder failed: {report}")
    return report


def build_input(dataset: Path, output: Path, provenance: Path,
                env: dict[str, str]) -> dict[str, Any]:
    result = run([sys.executable, str(RCA_BUILDER), "--dataset", str(dataset),
                  "--output", str(output), "--provenance", str(provenance)], env=env)
    report = json_output(result, "RCA Input builder")
    if report.get("status") != "PASS":
        raise IntegratedRunError(f"RCA Input contract failed: {report}")
    return report


def facts_from(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0" or not isinstance(data.get("facts"), list):
        raise IntegratedRunError(f"invalid Dataset {path}")
    return data["facts"]


def validate_fault_facts(fault: str, facts_a: list[dict[str, Any]],
                         facts_b: list[dict[str, Any]],
                         baseline_a: list[dict[str, Any]] | None = None,
                         baseline_b: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    def select(facts, predicate):
        return [item for item in facts if predicate(item)]
    def edge_to(item: dict[str, Any], target: str) -> bool:
        if str(item.get("target", "")).lower() == target.lower():
            return True
        # Scenario A's conventional client span encodes the peer in its native RPC
        # method name (for example hipstershop.PaymentService/Charge).
        operation = str(item.get("operation", "")).lower()
        compact_target = target.lower().removesuffix("service")
        return compact_target in operation
    evidence: dict[str, Any] = {"fault": fault, "scenarios": {}}
    for side, facts in (("A", facts_a), ("B", facts_b)):
        if fault == "F1":
            cpu = select(facts, lambda x: x.get("signal") == "metric" and
                         x.get("metric") == "cpu_utilization" and x.get("service") == "checkoutservice" and
                         x.get("availability") == "OBSERVED")
            duration = select(facts, lambda x: x.get("signal") == "metric" and
                              x.get("metric") == "request_duration" and x.get("service") == "checkoutservice" and
                              x.get("value", {}).get("availability") == "OBSERVED")
            baseline = baseline_a if side == "A" else baseline_b
            baseline_cpu = select(baseline or [], lambda x: x.get("metric") == "cpu_utilization" and
                                  x.get("service") == "checkoutservice" and x.get("availability") == "OBSERVED")
            baseline_duration = select(baseline or [], lambda x: x.get("metric") == "request_duration" and
                                        x.get("service") == "checkoutservice" and
                                        x.get("value", {}).get("availability") == "OBSERVED")
            cpu_value = max((x["value"] for x in cpu if isinstance(x.get("value"), (int, float))), default=None)
            normal_cpu = max((x["value"] for x in baseline_cpu if isinstance(x.get("value"), (int, float))), default=None)
            fault_by_operation = {
                x.get("operation"): x["value"]["mean_seconds"] for x in duration
                if x.get("operation") and isinstance(x.get("value", {}).get("mean_seconds"), (int, float))
            }
            baseline_by_operation = {
                x.get("operation"): x["value"]["mean_seconds"] for x in baseline_duration
                if x.get("operation") and isinstance(x.get("value", {}).get("mean_seconds"), (int, float))
            }
            degraded_operations = {
                operation: {"fault": fault_by_operation[operation], "normal": baseline_by_operation[operation]}
                for operation in fault_by_operation.keys() & baseline_by_operation.keys()
                if fault_by_operation[operation] > baseline_by_operation[operation]
            }
            if cpu_value is None or normal_cpu is None or cpu_value <= normal_cpu:
                raise IntegratedRunError(f"{fault}/{side}: checkoutservice CPU did not exceed normal baseline")
            if not degraded_operations:
                raise IntegratedRunError(f"{fault}/{side}: checkoutservice request duration did not exceed normal baseline")
            evidence["scenarios"][side] = {"cpu_cores": cpu_value, "normal_cpu_cores": normal_cpu,
                                            "request_duration_degraded_operations": degraded_operations,
                                            "status": "PASS"}
        elif fault == "F2":
            spans = select(facts, lambda x: x.get("signal") == "trace" and x.get("service") == "checkoutservice" and
                           x.get("kind") == "CLIENT" and edge_to(x, "paymentservice"))
            durations = [x.get("duration_ms") for x in spans if isinstance(x.get("duration_ms"), (int, float))]
            if not durations or max(durations) < 100:
                raise IntegratedRunError(f"{fault}/{side}: checkoutservice->paymentservice latency < 100ms or absent")
            evidence["scenarios"][side] = {"edge_span_count": len(spans), "max_duration_ms": max(durations), "status": "PASS"}
        elif fault in {"F3", "F5"}:
            service, target = (("checkoutservice", "paymentservice") if fault == "F3"
                               else ("cartservice", "redis-cart"))
            spans = select(facts, lambda x: x.get("signal") == "trace" and x.get("service") == service and
                           edge_to(x, target) and x.get("outcome") == "ERROR")
            if not spans:
                raise IntegratedRunError(f"{fault}/{side}: failed dependency edge {service}->{target} absent")
            evidence["scenarios"][side] = {"error_edge_span_count": len(spans), "status": "PASS"}
        elif fault == "F4":
            logs = select(facts, lambda x: x.get("signal") == "log" and x.get("service") == "paymentservice" and
                          x.get("message") == "Payment charge failed" and x.get("severity") == "ERROR")
            spans = select(facts, lambda x: x.get("signal") == "trace" and x.get("service") == "paymentservice" and
                           "Charge" in x.get("operation", "") and x.get("kind") == "SERVER" and x.get("outcome") == "ERROR")
            if len(logs) != 1 or not spans:
                raise IntegratedRunError(f"{fault}/{side}: Charge error log/span missing or duplicated")
            if side == "A" and ({"trace_id", "span_id"} & set(logs[0])):
                raise IntegratedRunError("F4/A conventional log unexpectedly has trace correlation")
            if side == "B" and not any(logs[0].get("trace_id") == span.get("trace_id") and
                                       logs[0].get("span_id") == span.get("span_id") for span in spans):
                raise IntegratedRunError("F4/B log does not correlate to a real Charge error span")
            evidence["scenarios"][side] = {"error_log_count": len(logs), "error_charge_span_count": len(spans),
                                            "log_trace_correlated": side == "B", "status": "PASS"}
    return evidence


def verify_gt(fault: str, gt_path: Path) -> dict[str, Any]:
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    expected_type, expected_location, expected_kind = FAULTS[fault]
    if (gt.get("fault_type") != expected_type or gt.get("root_location", {}).get("value") != expected_location or
            gt.get("root_location", {}).get("kind") != expected_kind or gt.get("status") != "RECOVERED" or
            gt.get("execution_eligible") is not True):
        raise IntegratedRunError(f"{fault}: independent Ground Truth is invalid/ineligible: {gt}")
    if parse_time(gt["confirmed_start"]) >= parse_time(gt["confirmed_end"]):
        raise IntegratedRunError(f"{fault}: invalid Ground Truth time interval")
    return gt


def hash_inventory(root: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "hashes.sha256"):
        data = path.read_bytes()
        entries.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data)})
    return entries


def verify_hash_manifest(root: Path) -> int:
    hash_path = root / "hashes.sha256"
    if not hash_path.is_file():
        raise IntegratedRunError(f"missing hash manifest: {hash_path}")
    expected = {}
    for line in hash_path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    actual = {path.relative_to(root).as_posix(): path for path in root.rglob("*")
              if path.is_file() and path != hash_path}
    if actual.keys() != expected.keys():
        raise IntegratedRunError(
            f"artifact inventory mismatch under {root}: "
            f"unlisted={sorted(actual.keys() - expected.keys())}, "
            f"missing={sorted(expected.keys() - actual.keys())}"
        )
    mismatches = [relative for relative, path in actual.items()
                  if hashlib.sha256(path.read_bytes()).hexdigest() != expected[relative]]
    if mismatches:
        raise IntegratedRunError(f"artifact hashes mismatch under {root}: {mismatches}")
    return len(expected)


def verify_global_raw_hashes(root: Path) -> int:
    hash_path = root / "global-raw-hashes.sha256"
    if not hash_path.is_file():
        raise IntegratedRunError("global RAW collection hash list is missing")
    entries = {}
    for line in hash_path.read_text(encoding="utf-8").splitlines():
        try:
            digest, relative = line.split("  ", 1)
        except ValueError as exc:
            raise IntegratedRunError(f"invalid global RAW hash entry: {line!r}") from exc
        relative_path = Path(relative)
        if (not relative or relative_path.is_absolute() or ".." in relative_path.parts
                or relative in entries):
            raise IntegratedRunError(f"invalid or duplicate global RAW path: {relative!r}")
        path = root / relative
        if relative == "manifest.json":
            path = root / "global-raw-manifest.json"
        if (path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise IntegratedRunError(f"global RAW hash mismatch: {relative}")
        entries[relative] = digest

    manifest_path = root / "global-raw-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegratedRunError(f"invalid global RAW manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise IntegratedRunError("global RAW manifest must be a JSON object")
    declared = {}
    for section in ("native_responses", "control_artifacts"):
        artifacts = manifest.get(section)
        if not isinstance(artifacts, list):
            raise IntegratedRunError(f"global RAW manifest has invalid {section}")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise IntegratedRunError(f"global RAW manifest has invalid {section} entry")
            relative = artifact.get("path")
            digest = artifact.get("sha256")
            size = artifact.get("bytes")
            if (not isinstance(relative, str) or not relative or relative in declared
                    or not isinstance(digest, str) or not isinstance(size, int)):
                raise IntegratedRunError(f"global RAW manifest has invalid artifact metadata: {artifact}")
            declared[relative] = (digest, size)
    expected_paths = set(declared) | {"manifest.json"}
    if set(entries) != expected_paths:
        raise IntegratedRunError(
            "global RAW manifest/hash list inventory mismatch: "
            f"unlisted={sorted(set(entries) - expected_paths)}, "
            f"missing={sorted(expected_paths - set(entries))}"
        )
    for relative, (digest, size) in declared.items():
        path = root / relative
        if (entries[relative] != digest or path.is_symlink() or not path.is_file()
                or path.stat().st_size != size):
            raise IntegratedRunError(f"global RAW manifest mismatch: {relative}")
    return len(entries)


def package_global_raw(global_raw: Path, staging: Path) -> int:
    """Copy the complete native RAW bundle into results staging without rewriting it."""
    for directory in ("datasetA", "datasetB", "control"):
        source = global_raw / directory
        if not source.is_dir():
            raise IntegratedRunError(f"global RAW directory is missing: {source}")
        shutil.copytree(source, staging / directory, copy_function=shutil.copy2)
    shutil.copy2(global_raw / "manifest.json", staging / "global-raw-manifest.json")
    shutil.copy2(global_raw / "hashes.sha256", staging / "global-raw-hashes.sha256")
    return verify_global_raw_hashes(staging)


def raw_log_sample(bundle: Path, side: str, predicate) -> dict[str, Any] | None:
    for path in sorted((bundle / f"dataset{side}" / "logs").rglob("*.json")):
        response = json.loads(path.read_text(encoding="utf-8"))
        for stream in response.get("data", {}).get("result", []):
            for timestamp, line in stream.get("values", []):
                if predicate(stream.get("stream", {}), line):
                    return {"raw_file": path.relative_to(bundle).as_posix(),
                            "stream": stream.get("stream", {}),
                            "timestamp": timestamp, "line": line}
    return None


def raw_trace_sample(bundle: Path, side: str, predicate) -> dict[str, Any] | None:
    for path in trace_chunk_paths(bundle, side):
        response = json.loads(path.read_text(encoding="utf-8"))
        for trace in response.get("data", []):
            for span in trace.get("spans", []):
                process = (trace.get("processes", {}) or {}).get(span.get("processID"), {})
                if predicate(process.get("serviceName"), span):
                    return {"raw_file": path.relative_to(bundle).as_posix(),
                            "trace_id": trace.get("traceID"), "service": process.get("serviceName"),
                            "span": span}
    return None


def raw_metric_sample(bundle: Path, side: str) -> dict[str, Any] | None:
    response = json.loads((bundle / f"dataset{side}/metrics.json").read_text(encoding="utf-8"))
    for row in response.get("data", {}).get("result", []):
        if row.get("metric", {}).get("service_name") in {
                "frontend", "checkoutservice", "cartservice", "paymentservice"}:
            return row
    return None


def dataset_evidence(result_root: Path, fault_dir: str, side: str, fault: str,
                     signal: str) -> list[dict[str, Any]]:
    path = result_root / fault_dir / "dataset" / f"dataset-{side}.json"
    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    if fault == "F4" and signal == "log":
        return [x for x in facts if x.get("signal") == "log" and x.get("service") == "paymentservice"
                and x.get("message") == "Payment charge failed"]
    if fault == "F4" and signal == "trace":
        return [x for x in facts if x.get("signal") == "trace" and x.get("service") == "paymentservice"
                and "Charge" in x.get("operation", "") and x.get("outcome") == "ERROR"]
    if fault == "F5" and signal == "trace":
        return [x for x in facts if x.get("signal") == "trace" and x.get("service") == "cartservice"
                and (x.get("target") == "redis-cart" or
                     "redis-cart" in str(x.get("operation", "")).lower())
                and x.get("outcome") == "ERROR"]
    if fault == "F5" and signal == "log":
        return [x for x in facts if x.get("signal") == "log" and x.get("service") == "cartservice"
                and ("RedisConnectionException" in x.get("message", "") or
                     "RedisConnectionException" in json.dumps(x.get("attributes", {})))]
    return []


def dataset_and_input_chain(result_root: Path, fault_dir: str, fault: str, side: str) -> dict[str, Any]:
    signal = "log" if fault == "F4" else "trace"
    ds_facts = dataset_evidence(result_root, fault_dir, side, fault, signal)
    input_path = result_root / fault_dir / f"rca-input-{side}.json"
    input_facts = json.loads(input_path.read_text(encoding="utf-8"))["evidence"]
    selected_ids = {fact["id"] for fact in ds_facts}
    return {"dataset_file": f"{fault_dir}/dataset/dataset-{side}.json",
            "dataset_evidence": ds_facts,
            "rca_input_file": f"{fault_dir}/rca-input-{side}.json",
            "rca_input_evidence": [fact for fact in input_facts if fact.get("id") in selected_ids]}


def write_hashes(root: Path) -> list[str]:
    lines = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "hashes.sha256"):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}")
    (root / "hashes.sha256").write_text("\n".join(lines) + "\n")
    return lines


def create_audit_package(results: Path, execution: dict[str, Any], fault_results: dict[str, Any],
                         semantic: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    audit = EXPERIMENT / "teste"
    stage_md = EXPERIMENT / "stages/12-final-experiment-validation.md"
    stage_json = EXPERIMENT / "stages/12-final-experiment-validation.json"
    if audit.exists() or stage_md.exists() or stage_json.exists():
        raise IntegratedRunError("final audit package or stage record already exists; refusing overwrite")
    (audit / "faults").mkdir(parents=True)
    (audit / "samples").mkdir()
    (audit / "validation").mkdir()
    (audit / "validation/dataset-provenance").mkdir()
    global_raw = results
    for side in "AB":
        for signal in ("metrics", "logs", "traces"):
            if signal == "metrics":
                sample = raw_metric_sample(global_raw, side)
                sample_path = f"dataset{side}/metrics.json"
            elif signal == "logs":
                sample = raw_log_sample(global_raw, side, lambda _labels, _line: True)
                sample_path = "native backend log response"
            else:
                sample = raw_trace_sample(global_raw, side, lambda _service, _span: True)
                sample_path = "native backend trace response"
            if sample is None:
                raise IntegratedRunError(f"global RAW has no representative {signal} sample for {side}")
            (audit / f"samples/{signal}-{side}.json").write_text(
                json.dumps({"raw_file": sample_path, "sample": sample}, indent=2, sort_keys=True) + "\n")

    for fault, item in fault_results.items():
        fault_dir = item["result_directory"]
        result_dir = results / fault_dir
        ground_truth = json.loads((result_dir / "ground-truth.json").read_text(encoding="utf-8"))
        summary: dict[str, Any] = {
            "fault": fault, "fault_type": ground_truth["fault_type"],
            "location": ground_truth["root_location"], "confirmed_start": ground_truth["confirmed_start"],
            "confirmed_end": ground_truth["confirmed_end"],
            "recovery": ground_truth["recovery_confirmation"], "cleanup": item["status"],
            "raw_bundle": f"docs/experiment/results/{fault_dir}/raw",
            "dataset_files": {side: f"{fault_dir}/dataset/dataset-{side}.json" for side in "AB"},
            "rca_input_files": {side: f"{fault_dir}/rca-input-{side}.json" for side in "AB"},
            "validation": item["evidence"],
        }
        if fault in {"F4", "F5"}:
            chain = {}
            bundle = result_dir / "raw"
            for side in "AB":
                if fault == "F4":
                    raw_log = raw_log_sample(bundle, side, lambda labels, line:
                                             labels.get("service_name") == "paymentservice" and
                                             "Payment charge failed" in line)
                    raw_trace = raw_trace_sample(bundle, side, lambda service, span:
                                                 service == "paymentservice" and
                                                 "Charge" in str(span.get("operationName", "")) and
                                                 any(tag.get("key") in {"error", "result", "rpc.grpc.status_code"}
                                                     for tag in span.get("tags", [])))
                else:
                    raw_log = raw_log_sample(bundle, side, lambda labels, line:
                                             labels.get("service_name") == "cartservice" and
                                             "RedisConnectionException" in line)
                    raw_trace = raw_trace_sample(bundle, side, lambda service, span:
                                                 service == "cartservice" and
                                                 ("redis-cart" in str(span.get("operationName", "")).lower() or
                                                  any(tag.get("key") == "server.address" and tag.get("value") == "redis-cart"
                                                      for tag in span.get("tags", []))) and
                                                 any((tag.get("key") == "error" and tag.get("value") is True) or
                                                     (tag.get("key") == "result" and
                                                      str(tag.get("value", "")).upper() == "ERROR") or
                                                     (tag.get("key") in {"otel.status_code", "status.code"} and
                                                      str(tag.get("value", "")).upper() == "ERROR")
                                                     for tag in span.get("tags", [])))
                chain[side] = {"raw_application_log": raw_log, "raw_trace": raw_trace,
                               **dataset_and_input_chain(results, fault_dir, fault, side)}
                if (fault == "F4" and not raw_log) or not raw_trace:
                    raise IntegratedRunError(f"{fault}/{side}: audit package lacks required representative RAW evidence")
                if not chain[side]["dataset_evidence"] or not chain[side]["rca_input_evidence"]:
                    raise IntegratedRunError(f"{fault}/{side}: representative fact is missing from Dataset or RCA Input")
                source = result_dir / "dataset" / "provenance.json"
                shutil.copy2(source, audit / f"validation/dataset-provenance/{fault}-{side}.json")
            summary["representative_chain"] = chain
        (audit / f"faults/{fault}.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    provenance_summary = {}
    for fault, item in fault_results.items():
        provenance_summary[fault] = {
            side: f"validation/dataset-provenance/{fault}-{side}.json" for side in "AB"
        }
    (audit / "validation/dataset-provenance.json").write_text(
        json.dumps({"status": "PASS", "faults": provenance_summary}, indent=2, sort_keys=True) + "\n"
    )

    shutil.copy2(results / "execution.json", audit / "execution.json")
    (audit / "deployment-provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    shutil.copy2(results / "semantic-validation.json", audit / "validation/semantic-coverage.json")
    shutil.copy2(results / "fault-validation.json", audit / "validation/fault-coverage.json")
    results_hashes = (results / "hashes.sha256").read_bytes()
    (audit / "validation/artifact-integrity.json").write_text(json.dumps({
        "status": "PASS",
        "results_manifest_sha256": hashlib.sha256((results / "manifest.json").read_bytes()).hexdigest(),
        "results_hash_list_sha256": hashlib.sha256(results_hashes).hexdigest(),
        "results_hashed_file_count": len(results_hashes.decode("utf-8").splitlines()),
        "results_hashes_include_manifest": True,
        "audit_hashes_exclude_self": True,
    }, indent=2, sort_keys=True) + "\n")
    (audit / "validation/leakage.json").write_text(json.dumps({
        "status": "PASS", "rca_inputs_created_without_ground_truth": True,
        "scenario_label_in_payload": False, "model_invocations": 0,
        "evidence": "the input builder accepted Dataset only; each payload is an identity projection",
    }, indent=2, sort_keys=True) + "\n")
    raw_summary = {}
    for label, bundle, manifest_name, hashes_name in [
        ("normal", results / "normal-smoke/raw", "manifest.json", "hashes.sha256"),
        ("global", results, "global-raw-manifest.json", "global-raw-hashes.sha256"),
    ]:
        manifest = json.loads((bundle / manifest_name).read_text(encoding="utf-8"))
        raw_summary[label] = {"window": manifest["window"], "file_count": len(manifest.get("native_responses", [])),
                              "manifest_sha256": hashlib.sha256((bundle / manifest_name).read_bytes()).hexdigest(),
                              "hash_list_sha256": hashlib.sha256((bundle / hashes_name).read_bytes()).hexdigest()}
    for fault, item in fault_results.items():
        bundle = results / item["result_directory"] / "raw"
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        raw_summary[fault] = {"window": manifest["window"],
                              "manifest_sha256": hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest(),
                              "hash_list_sha256": hashlib.sha256((bundle / "hashes.sha256").read_bytes()).hexdigest()}
    (audit / "raw-summary.json").write_text(json.dumps(raw_summary, indent=2, sort_keys=True) + "\n")
    dataset_summary = {}
    for fault, item in fault_results.items():
        directory = results / item["result_directory"] / "dataset"
        manifests = {side: json.loads((directory / f"build-manifest.json").read_text()) for side in "AB"}
        dataset_summary[fault] = {side: manifests[side]["fact_counts"][side] for side in "AB"}
    (audit / "dataset-summary.json").write_text(json.dumps(dataset_summary, indent=2, sort_keys=True) + "\n")
    versions = load_env_file(VERSIONS)
    environment = {"pinned": versions, "kind": run([os.environ.get("KIND_BIN", "kind"), "version"]).stdout.strip(),
                   "kubectl_client": json_output(run([os.environ.get("KUBECTL_BIN", "kubectl"),
                                                       "version", "--client", "-o", "json"]), "kubectl version")["clientVersion"],
                   "docker_server": run(["docker", "version", "--format", "{{.Server.Version}}"] ).stdout.strip()}
    (audit / "environment.json").write_text(json.dumps(environment, indent=2, sort_keys=True) + "\n")
    (audit / "README.md").write_text(
        "# Pacote de auditoria independente — experiment-v2\n\n"
        "Este pacote contém metadados de execução, hashes, amostras nativas e cadeias representativas F4/F5. "
        "O RAW completo permanece em `docs/experiment/results/`; as amostras apontam para seus arquivos de origem. "
        "Ground Truth aparece somente nos arquivos de auditoria de fault e não foi incluído nos RCA Inputs.\n\n"
        "`hashes.sha256` cobre todos os arquivos do pacote, exceto ele próprio. O manifest explica que os hashes dos "
        "bundles RAW completos estão nos próprios bundles e são referenciados em `raw-summary.json`. "
        "A integridade dos resultados é resumida em `validation/artifact-integrity.json`.\n"
    )
    package_manifest = {"format": "tcc-experiment-v2-independent-audit-v1",
                        "tested_commit": execution["tested_commit"],
                        "artifacts": hash_inventory(audit),
                        "raw_full_data_location": "docs/experiment/results/",
                        "hash_exclusion": "hashes.sha256 does not hash itself"}
    (audit / "manifest.json").write_text(json.dumps(package_manifest, indent=2, sort_keys=True) + "\n")
    package_hashes = write_hashes(audit)
    verify_hash_manifest(audit)
    package_size = sum(path.stat().st_size for path in audit.rglob("*") if path.is_file())
    if package_size > 25 * 1024 * 1024:
        raise IntegratedRunError(f"independent audit package exceeds preferred 25 MiB target: {package_size}")
    final_record = {
        "tested_commit": execution["tested_commit"], "branch": "experiment-v2",
        "overall": "SUCCESS", "stages": {str(i): "PASS" for i in range(1, 13)},
        "faults": {fault: item["evidence"] for fault, item in fault_results.items()},
        "scientific_invariants": {"same_physical_execution": True, "diagnostic_coverage": True,
                                  "backend_isolation": True, "ground_truth_independent": True,
                                  "deployment_provenance": True, "raw_to_dataset_provenance": True,
                                  "dataset_to_rca_input_provenance": True,
                                  "experiment_metadata_leakage": False,
                                  "post_hoc_parity_fabrication": False},
        "results": "docs/experiment/results/", "audit_package": "docs/experiment/teste/",
        "audit_package_bytes": package_size, "audit_hash_entries": len(package_hashes),
        "llm_invocations": 0,
    }
    stage_json = EXPERIMENT / "stages/12-final-experiment-validation.json"
    stage_md = EXPERIMENT / "stages/12-final-experiment-validation.md"
    stage_json.parent.mkdir(parents=True, exist_ok=True)
    stage_json.write_text(json.dumps(final_record, indent=2, sort_keys=True) + "\n")
    stage_md.write_text(
        "# Etapa 12 — validação integrada F1–F5\n\n"
        f"**SUCCESS.** Bateria executada no commit `{execution['tested_commit']}`; nenhuma chamada de LLM foi feita.\n\n"
        f"- Resultados completos e RAW: [`results/`](../results/).\n"
        f"- Pacote independente: [`teste/`](../teste/), {package_size} bytes, hashes verificados.\n"
        "- F1–F5: fault confirmada pelo control plane, recuperação/cleanup, RAW, Dataset e RCA Input validados.\n"
        "- A/B observaram a mesma execução; os sinais nativos foram preservados, sem equalização post-hoc.\n"
        "- Ground Truth ficou fora dos RCA Inputs; proveniência e hashes foram verificados.\n"
        "- O histórico em `reports/` conserva blockers/incidentes reais das etapas de desenvolvimento.\n"
    )
    return {"status": "PASS", "path": str(audit), "bytes": package_size,
            "files_hashed": len(package_hashes), "hashes_sha256": hashlib.sha256((audit / "hashes.sha256").read_bytes()).hexdigest()}


def finalize_results(staging: Path, output: Path, execution: dict[str, Any]) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise IntegratedRunError(f"results output already exists; refusing overwrite: {output}")
    manifest = {"format": "tcc-experiment-v2-integrated-results-v1", **execution,
                "artifacts": hash_inventory(staging)}
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    lines = []
    for path in sorted(item for item in staging.rglob("*") if item.is_file() and item.name != "hashes.sha256"):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(staging).as_posix()}")
    (staging / "hashes.sha256").write_text("\n".join(lines) + "\n")
    verify_hash_manifest(staging)

    output.parent.mkdir(parents=True, exist_ok=True)
    publish_staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.publish-", dir=output.parent))
    try:
        shutil.copytree(staging, publish_staging, dirs_exist_ok=True, copy_function=shutil.copy2)
        verify_hash_manifest(publish_staging)
        if output.exists() or output.is_symlink():
            raise IntegratedRunError(f"results output appeared during publication: {output}")
        os.replace(publish_staging, output)
    finally:
        if publish_staging.exists():
            shutil.rmtree(publish_staging)
    return {"status": "PASS", "results": str(output), "files_hashed": len(lines),
            "manifest_sha256": hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest(),
            "hash_list_sha256": hashlib.sha256((output / "hashes.sha256").read_bytes()).hexdigest()}


def execute(output: Path, active_seconds: float = MIN_FAULT_ACTIVE_SECONDS) -> dict[str, Any]:
    branch, commit, status = git_state()
    if status.strip():
        raise IntegratedRunError(f"working tree must be clean before battery:\n{status}")
    if output.resolve() != (EXPERIMENT / "results").resolve():
        raise IntegratedRunError(f"integrated battery output must be {EXPERIMENT / 'results'}")
    if output.exists():
        raise IntegratedRunError(f"results path already exists: {output}")
    envvars = load_env_file(VERSIONS)
    env = os.environ.copy()
    env.setdefault("KIND_BIN", "kind")
    env.setdefault("KUBECTL_BIN", "kubectl")
    with PreserveWorkOnError() as work:
        staging = work / "results"
        staging.mkdir()
        preflight_report = preflight(work / "preflight", branch, commit, envvars)
        (staging / "preflight.json").write_text(json.dumps(preflight_report, indent=2, sort_keys=True) + "\n")

        global_start_ns = time.time_ns()
        start_time = datetime_from_epoch_ns(global_start_ns)
        normal_record = work / "normal-workload.json"
        normal_summary = run_normal_baseline(envvars, env, normal_record)
        normal_start = normal_summary["measurement_started_at"]
        normal_end = time_text(parse_time(normal_summary["finished_at"]) + timedelta(seconds=20))
        normal_raw = work / "normal-raw"
        normal_collection = collect(normal_raw, normal_start, normal_end, normal_record,
                                    commit, envvars, env)
        normal_dataset = work / "normal-dataset"
        normal_build = build_dataset(normal_raw, normal_dataset, env)
        normal_facts = {side: facts_from(normal_dataset / f"dataset-{side}.json") for side in "AB"}
        shutil.copytree(normal_raw, staging / "normal-smoke/raw", copy_function=shutil.copy2)
        shutil.copytree(normal_dataset, staging / "normal-smoke/dataset", copy_function=shutil.copy2)
        (staging / "normal-smoke/collection.json").write_text(json.dumps(normal_collection, indent=2, sort_keys=True) + "\n")

        fault_results: dict[str, Any] = {}
        for ordinal, fault in enumerate(FAULTS, start=1):
            fault_work = work / fault
            fault_work.mkdir()
            gt_path = fault_work / "ground-truth.json"
            report_path = fault_work / "fault-smoke.json"
            result = run([sys.executable, "-m", "scripts.experiment.fault_smoke",
                          "--fault", fault, "--record", str(gt_path),
                          "--active-seconds", str(active_seconds)], env=env, check=False)
            try:
                report = json_output(result, f"{fault} fault smoke")
            except IntegratedRunError as exc:
                raise IntegratedRunError(
                    f"{fault} fault smoke did not return JSON (exit={result.returncode}); "
                    f"stdout={result.stdout[-2000:]!r}; stderr={result.stderr[-4000:]!r}"
                ) from exc
            report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            if result.returncode or report.get("status") != "PASS":
                raise IntegratedRunError(f"{fault} injection/recovery smoke failed: {report}")
            check_no_residual_fault_state(env.get("KUBECTL_BIN", "kubectl"),
                                          f"kind-{envvars['KIND_CLUSTER_NAME']}",
                                          envvars["SUT_NAMESPACE"])
            gt = verify_gt(fault, gt_path)
            workload_record = fault_work / "workload.json"
            workload_record.write_text(json.dumps(report.get("active_probe"), indent=2, sort_keys=True) + "\n")
            raw = fault_work / "raw"
            collected = collect(raw, gt["confirmed_start"], gt["confirmed_end"], workload_record,
                                commit, envvars, env)
            dataset_dir = fault_work / "dataset"
            built = build_dataset(raw, dataset_dir, env)
            inputs = {}
            facts = {}
            for side in "AB":
                dataset_path = dataset_dir / f"dataset-{side}.json"
                facts[side] = facts_from(dataset_path)
                inputs[side] = build_input(dataset_path, fault_work / f"rca-input-{side}.json",
                                           fault_work / f"rca-input-provenance-{side}.json", env)
            evidence = validate_fault_facts(fault, facts["A"], facts["B"],
                                            normal_facts["A"], normal_facts["B"])
            name = f"{ordinal:03d}_{FAULTS[fault][0]}_r01"
            target = staging / name
            target.mkdir()
            shutil.copytree(raw, target / "raw", copy_function=shutil.copy2)
            shutil.copytree(dataset_dir, target / "dataset", copy_function=shutil.copy2)
            for side in "AB":
                shutil.copy2(dataset_dir / f"dataset-{side}.json", target / f"dataset{side}.json")
            shutil.copy2(gt_path, target / "ground-truth.json")
            shutil.copy2(report_path, target / "fault-smoke.json")
            for side in "AB":
                shutil.copy2(fault_work / f"rca-input-{side}.json", target / f"rca-input-{side}.json")
                shutil.copy2(fault_work / f"rca-input-provenance-{side}.json",
                             target / f"rca-input-provenance-{side}.json")
            fault_results[fault] = {"status": "PASS", "ground_truth": gt, "collection": collected,
                                    "dataset_build": built, "rca_input": inputs, "evidence": evidence,
                                    "result_directory": name}

        semantic_path = work / "semantic-validation.json"
        semantic = run_script(SEMANTIC_VALIDATOR,
                              "--normal-raw", str(normal_raw),
                              "--f4-raw", str(work / "F4/raw"),
                              "--f4-ground-truth", str(work / "F4/ground-truth.json"),
                              "--f5-raw", str(work / "F5/raw"),
                              "--f5-ground-truth", str(work / "F5/ground-truth.json"),
                              "--output", str(semantic_path), env=env)
        if semantic.get("status") != "PASS":
            raise IntegratedRunError(f"RAW semantic validation failed: {semantic}")
        shutil.copy2(semantic_path, staging / "semantic-validation.json")

        global_end_ns = time.time_ns()
        global_end = datetime_from_epoch_ns(global_end_ns)
        control_workload = work / "battery-workloads.json"
        control_workload.write_text(json.dumps({"normal": normal_summary,
                                                "faults": {key: value["evidence"] for key, value in fault_results.items()}},
                                               indent=2, sort_keys=True) + "\n")
        global_raw = work / "global-raw"
        global_collection = collect(global_raw, time_text(start_time), time_text(global_end),
                                    control_workload, commit, envvars, env,
                                    start_ns=global_start_ns, end_ns=global_end_ns)
        (staging / "global-collection.json").write_text(json.dumps(global_collection, indent=2, sort_keys=True) + "\n")
        package_global_raw(global_raw, staging)
        (staging / "README.md").write_text(
            "# Resultados integrados — experiment-v2\n\n"
            f"Commit executado: `{commit}`. RAW global completo e nativo está em `datasetA/`, `datasetB/` e `control/`; "
            "`global-raw-manifest.json` e `global-raw-hashes.sha256` preservam a consulta e a integridade da coleta.\n\n"
            "Cada diretório numerado contém RAW da janela do fault, Dataset A/B, RCA Input A/B e Ground Truth "
            "separado dos inputs. Os inputs não contêm Ground Truth nem rótulo de cenário. Nenhuma chamada de LLM foi feita.\n"
        )

        tested_end = utc_now()
        execution = {"tested_commit": commit, "branch": branch,
                     "experiment_start": time_text(start_time), "experiment_end": time_text(tested_end),
                     "experiment_start_ns": str(global_start_ns),
                     "experiment_end_ns": str(global_end_ns),
                     "fault_order": list(FAULTS), "workload_configuration": normal_summary["configuration"],
                     "deployment_provenance_status": "PASS", "normal_workload_status": "PASS",
                     "faults": {key: value["status"] for key, value in fault_results.items()},
                     "semantic_validation_status": semantic["status"], "llm_invocations": 0}
        (staging / "execution.json").write_text(json.dumps(execution, indent=2, sort_keys=True) + "\n")
        (staging / "fault-validation.json").write_text(json.dumps(fault_results, indent=2, sort_keys=True) + "\n")
        integrity = finalize_results(staging, output, execution)
        audit = create_audit_package(output, execution, fault_results, semantic,
                                     preflight_report["deployment_provenance"])
        return {"status": "PASS", "tested_commit": commit, "faults": fault_results,
                "normal_raw": str(output / "normal-smoke/raw"),
                "global_raw": str(output / "global-raw"),
                "semantic_validation": str(output / "semantic-validation.json"),
                "integrity": integrity, "audit_package": audit}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--active-seconds", type=float, default=MIN_FAULT_ACTIVE_SECONDS)
    args = parser.parse_args()
    if args.active_seconds < MIN_FAULT_ACTIVE_SECONDS:
        parser.error(f"active-seconds must be at least {MIN_FAULT_ACTIVE_SECONDS:g}; "
                     f"cAdvisor scrape interval is {PROMETHEUS_CADVISOR_SCRAPE_SECONDS:g}s")
    try:
        result = execute(args.output_dir.resolve(), args.active_seconds)
    except (IntegratedRunError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        reports = EXPERIMENT / "reports"
        report_md = reports / "BLOCKED-12-integrated-validation.md"
        report_json = reports / "BLOCKED-12-integrated-validation.json"
        evidence_path = None
        try:
            reports.mkdir(parents=True, exist_ok=True)
            if LAST_WORK_DIR and LAST_WORK_DIR.exists():
                evidence_path = reports / "BLOCKED-12-artifacts"
                if evidence_path.exists():
                    evidence_path = reports / f"BLOCKED-12-artifacts-{int(time.time())}"
                shutil.copytree(LAST_WORK_DIR, evidence_path, copy_function=shutil.copy2)
            branch, commit, status = git_state()
            detail = {
                "stage": "12-integrated-validation",
                "last_successful_commit": commit,
                "observed": str(exc),
                "expected": "normal workload, F1-F5, RAW, Dataset, RCA Input, semantic coverage and artifact integrity all PASS",
                "first_divergence": "integrated runner gate; inspect preserved evidence",
                "root_cause": "UNKNOWN",
                "evidence": str(evidence_path) if evidence_path else "no intermediate artifacts were produced",
                "affected_invariant": "the complete fault-to-RCA-input chain must pass on one frozen commit",
                "affected_files": [],
                "scientific_impact": "the integrated battery is not valid and must not be treated as a result",
                "safe_automatic_correction_possible": False,
                "recommended_correction": "diagnose the first failing layer from the preserved evidence before changing code/configuration",
                "physical_rerun_required": True,
                "working_tree": status,
                "branch": branch,
            }
            report_json.write_text(json.dumps(detail, indent=2, sort_keys=True) + "\n")
            report_md.write_text(
                "# BLOCKED — Etapa 12 integrated validation\n\n"
                f"- Último commit presente: `{commit}` (`{branch}`).\n"
                f"- Observado: `{exc}`\n"
                f"- Esperado: {detail['expected']}.\n"
                "- Primeira divergência: gate integrado; consulte a evidência preservada.\n"
                "- Root cause: UNKNOWN; não foi aplicada correção automática.\n"
                f"- Evidência: `{detail['evidence']}`.\n"
                f"- Relatório estruturado: `{report_json.relative_to(ROOT)}`.\n"
                f"- Working tree: `{status or 'CLEAN'}`.\n"
            )
        except Exception as report_error:
            print(f"Could not preserve blocker report: {report_error}", file=sys.stderr)
        print(f"Integrated experiment validation stopped: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
