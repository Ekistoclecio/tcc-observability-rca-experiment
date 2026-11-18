"""Apply one bounded fault smoke and record control-plane Ground Truth."""

from __future__ import annotations

import argparse
import json
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable
import urllib.error
import urllib.request

from scripts.experiment.ground_truth import GroundTruth, GroundTruthError


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "config/experiment/environment/versions.env"
FAULT_CONFIG = ROOT / "config/experiment/faults.json"
WORKLOAD = ROOT / "scripts/experiment/workload.py"
PROVENANCE_VALIDATOR = ROOT / "scripts/experiment/deployment/verify_provenance.py"
CONTAINER = "server"


class FaultSmokeError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"\'')
    return values


ENV = load_env_file(VERSIONS)
NAMESPACE = ENV["SUT_NAMESPACE"]
CLUSTER = ENV["KIND_CLUSTER_NAME"]
CONTEXT = f"kind-{CLUSTER}"
KIND = os.environ.get("KIND_BIN", "kind")
KUBECTL = os.environ.get("KUBECTL_BIN", "kubectl")
DOCKER = os.environ.get("DOCKER_BIN", "docker")


def command(
    args: list[str], *, input_text: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise FaultSmokeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result


def kube(*args: str, input_text: str | None = None, check: bool = True):
    return command(
        [KUBECTL, "--context", CONTEXT, *args], input_text=input_text, check=check
    )


def kube_json(*args: str) -> dict[str, Any]:
    return json.loads(kube(*args).stdout)


def require_exclusive_environment() -> None:
    clusters = command([KIND, "get", "clusters"]).stdout.split()
    if CLUSTER not in clusters:
        raise FaultSmokeError(f"experiment Kind cluster {CLUSTER} is absent")
    running = command(
        [
            "docker",
            "ps",
            "--filter",
            "label=io.x-k8s.kind.cluster",
            "--format",
            '{{.Label "io.x-k8s.kind.cluster"}}',
        ]
    ).stdout.split()
    if set(running) != {CLUSTER}:
        raise FaultSmokeError(f"non-exclusive active Kind clusters: {running}")


def deployment(name: str) -> dict[str, Any]:
    return kube_json("-n", NAMESPACE, "get", "deployment", name, "-o", "json")


def ready_replicas(name: str) -> int:
    return int(deployment(name).get("status", {}).get("readyReplicas", 0))


def desired_replicas(name: str) -> int:
    return int(deployment(name).get("spec", {}).get("replicas", 1))


def ready_endpoint_count(service: str) -> int:
    items = kube_json(
        "-n",
        NAMESPACE,
        "get",
        "endpointslices",
        "-l",
        f"kubernetes.io/service-name={service}",
        "-o",
        "json",
    ).get("items") or []
    return sum(
        len(endpoint.get("addresses", []))
        for item in items
        for endpoint in (item.get("endpoints") or [])
        if endpoint.get("conditions", {}).get("ready") is not False
    )


def parse_ipset_blocks(output: str) -> list[dict[str, Any]]:
    blocks = re.split(r"(?m)(?=^Name: )", output)
    parsed = []
    for block in blocks:
        name = re.search(r"(?m)^Name: (\S+)", block)
        if not name:
            continue
        set_type = re.search(r"(?m)^Type: (\S+)", block)
        members = re.search(r"(?ms)^Members:\s*\n(.*)", block)
        parsed.append({
            "name": name.group(1),
            "type": set_type.group(1) if set_type else None,
            "members": [line.strip() for line in (members.group(1).splitlines() if members else []) if line.strip()],
        })
    return parsed


def parse_classifier_packets(output: str, set_name: str) -> int:
    matches = []
    for line in output.splitlines():
        if f"match-set {set_name} " not in line or "CLASSIFY set 1:4" not in line:
            continue
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            matches.append(int(fields[0]))
    if len(matches) != 1:
        raise FaultSmokeError(
            f"expected one TC classifier for ipset {set_name}, found {len(matches)}"
        )
    return matches[0]


def active_target_sets(
    ipsets: list[dict[str, Any]], classifier_output: str, expected_member: str
) -> tuple[dict[str, Any], str]:
    referenced = {
        match.group(1)
        for line in classifier_output.splitlines()
        if "CLASSIFY set 1:4" in line
        for match in [re.search(r"match-set (\S+) ", line)]
        if match
    }
    candidates = []
    for parent_name in referenced:
        parent = next((item for item in ipsets if item["name"] == parent_name
                       and item["type"] == "list:set"), None)
        if parent is None:
            continue
        candidates.extend(
            (port_set, parent_name)
            for port_set in ipsets
            if port_set["type"] == "hash:net,port"
            and expected_member in port_set["members"]
            and port_set["name"] in parent["members"]
        )
    if len(candidates) != 1:
        raise FaultSmokeError(
            f"F2 must have one active VIP/port target ipset and classifier; "
            f"candidates={[(item[0]['name'], item[1]) for item in candidates]}"
        )
    return candidates[0]


def parse_netem_stats(output: str, latency_ms: int) -> dict[str, int]:
    blocks = re.split(r"(?m)(?=^qdisc )", output)
    matches = []
    for block in blocks:
        if not re.search(rf"^qdisc netem \S+ .*delay {latency_ms}ms(?:\s|$)", block, re.M):
            continue
        sent = re.search(r"Sent\s+(\d+) bytes\s+(\d+) pkt", block)
        if not sent:
            raise FaultSmokeError("netem qdisc has no parseable packet statistics")
        matches.append({"bytes": int(sent.group(1)), "packets": int(sent.group(2))})
    if len(matches) != 1:
        raise FaultSmokeError(
            f"expected one netem qdisc configured for {latency_ms}ms, found {len(matches)}"
        )
    return matches[0]


def checkout_netns_context(
    kubectl_bin: str = KUBECTL,
    context_name: str = CONTEXT,
    docker_bin: str = DOCKER,
) -> dict[str, Any]:
    pods = command([kubectl_bin, "--context", context_name, "-n", NAMESPACE,
                    "get", "pods", "-l", "app=checkoutservice", "-o", "json"]).stdout
    pods = json.loads(pods).get("items", [])
    pod = next((item for item in pods if item.get("status", {}).get("phase") == "Running"), None)
    if pod is None:
        raise FaultSmokeError("checkoutservice has no Running Pod for network confirmation")
    node = pod.get("spec", {}).get("nodeName")
    container = next(
        (item for item in pod.get("status", {}).get("containerStatuses", [])
         if item.get("name") == CONTAINER and item.get("containerID")),
        None,
    )
    if not node or container is None:
        raise FaultSmokeError("checkoutservice Pod lacks node/container identity")
    container_id = container["containerID"].split("://", 1)[-1]
    inspected = command([docker_bin, "exec", node, "crictl", "inspect", container_id])
    try:
        process_id = int(json.loads(inspected.stdout).get("info", {}).get("pid", 0))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise FaultSmokeError("cannot resolve checkoutservice host PID through CRI") from exc
    if process_id <= 0:
        raise FaultSmokeError("CRI returned an invalid checkoutservice host PID")
    daemons_result = command([kubectl_bin, "--context", context_name, "-n",
                              "chaos-mesh", "get", "pods", "-o", "json"])
    daemons = json.loads(daemons_result.stdout).get("items", [])
    daemon = next((item for item in daemons
                   if item.get("metadata", {}).get("name", "").startswith("chaos-daemon-")
                   and item.get("spec", {}).get("nodeName") == node
                   and item.get("status", {}).get("phase") == "Running"), None)
    if daemon is None:
        raise FaultSmokeError(f"no Running Chaos Daemon on checkoutservice node {node}")
    return {"pod": pod["metadata"]["name"], "node": node,
            "container_id": container_id, "pid": process_id,
            "chaos_daemon": daemon["metadata"]["name"]}


def checkout_netns_command(
    context: dict[str, Any], *args: str,
    kubectl_bin: str = KUBECTL,
    context_name: str = CONTEXT,
) -> str:
    result = command(
        [kubectl_bin, "--context", context_name, "-n", "chaos-mesh",
         "exec", context["chaos_daemon"], "--",
        "nsenter", "-t", str(context["pid"]), "-n", "--", *args,
        ], check=False,
    )
    if result.returncode:
        raise FaultSmokeError(
            f"network namespace command failed ({' '.join(args)}): {result.stderr.strip()}"
        )
    return result.stdout


def verify_checkout_network_effect_absent(
    kubectl_bin: str = KUBECTL,
    context_name: str = CONTEXT,
    docker_bin: str = DOCKER,
) -> dict[str, Any]:
    """Prove no active Chaos output rules or netem qdisc remain on checkout."""
    netns = checkout_netns_context(kubectl_bin, context_name, docker_bin)
    iptables = checkout_netns_command(
        netns, "iptables-save", "-c", kubectl_bin=kubectl_bin, context_name=context_name
    )
    active_chaos_rules = [
        line.strip() for line in iptables.splitlines()
        if line.startswith("-A ") and (
            "-A CHAOS-OUTPUT " in line or " -j CHAOS-OUTPUT" in line
        )
    ]
    qdisc = checkout_netns_command(
        netns, "tc", "-s", "qdisc", "show", "dev", "eth0",
        kubectl_bin=kubectl_bin, context_name=context_name,
    )
    netem_qdiscs = [line.strip() for line in qdisc.splitlines()
                    if line.lstrip().startswith("qdisc netem ")]
    if active_chaos_rules or netem_qdiscs:
        raise FaultSmokeError(
            "active checkout network fault effect remains: "
            f"iptables={active_chaos_rules}, netem={netem_qdiscs}"
        )
    return {
        "checkout_pod": netns["pod"],
        "active_chaos_iptables_rules": active_chaos_rules,
        "netem_qdiscs": netem_qdiscs,
        "status": "PASS",
    }


def f2_network_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    context = checkout_netns_context()
    ipset_names = checkout_netns_command(context, "ipset", "list", "-name").split()
    ipsets = []
    for name in ipset_names:
        output = checkout_netns_command(context, "ipset", "list", name)
        ipsets.extend(parse_ipset_blocks(output))
    target = state["external_target"]
    address, port_text = target.rsplit(":", 1)
    expected_member = f"{address},tcp:{port_text}"
    classifier = checkout_netns_command(context, "iptables", "-nvxL", "TC-TABLES-0")
    port_set, parent_name = active_target_sets(ipsets, classifier, expected_member)
    classifier_packets = parse_classifier_packets(classifier, parent_name)
    qdisc = checkout_netns_command(context, "tc", "-s", "qdisc", "show", "dev", "eth0")
    netem = parse_netem_stats(qdisc, state["latency_ms"])
    chaos_output = checkout_netns_command(context, "iptables", "-nvxL", "CHAOS-OUTPUT")
    first_rule = next((line.split() for line in chaos_output.splitlines()
                       if line.strip() and line.strip()[0].isdigit()), None)
    return {
        "observed_at": utc_now(),
        "network_context": context,
        "external_target": target,
        "ipset": {"name": port_set["name"], "member": expected_member,
                  "classifier_parent": parent_name},
        "classifier_packets": classifier_packets,
        "netem": netem,
        "chaos_output_packets": int(first_rule[0]) if first_rule else 0,
    }


def confirm_f2_network_effect(
    state: dict[str, Any], port: int, before: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    probe = run_workload("F2", port)
    after = f2_network_snapshot(state)
    deltas = {
        "classifier_packets": after["classifier_packets"] - before["classifier_packets"],
        "netem_packets": after["netem"]["packets"] - before["netem"]["packets"],
        "netem_bytes": after["netem"]["bytes"] - before["netem"]["bytes"],
    }
    if deltas["classifier_packets"] <= 0 or deltas["netem_packets"] <= 0:
        raise FaultSmokeError(f"F2 real-traffic confirmation failed: deltas={deltas}")
    return probe, {
        "control_plane_check": "F2 ClusterIP:port ipset classifier and netem packet counters increased",
        "evidence": {"external_target": state["external_target"], "before": before,
                     "after": after, "deltas": deltas,
                     "confirmation_probe_excluded_from_measurement_window": True},
    }


def verify_f2_network_recovered(state: dict[str, Any]) -> dict[str, Any]:
    def cleaned() -> tuple[bool, Any]:
        context = checkout_netns_context()
        qdisc = checkout_netns_command(
            context, "tc", "-s", "qdisc", "show", "dev", "eth0"
        )
        chaos_output = checkout_netns_command(context, "iptables", "-nvxL", "CHAOS-OUTPUT")
        classifier = checkout_netns_command(context, "iptables", "-nvxL", "TC-TABLES-0")
        latency_ms = state["latency_ms"]
        active_netem = bool(re.search(
            rf"(?m)^qdisc netem \S+ .*delay {latency_ms}ms(?:\s|$)", qdisc
        ))
        hook_active = any(
            line.strip().startswith(tuple("0123456789")) and "TC-TABLES-0" in line
            for line in chaos_output.splitlines()
        )
        chain_refs = re.search(r"(?m)^Chain TC-TABLES-0 \((\d+) references\)", classifier)
        classifier_referenced = chain_refs is None or int(chain_refs.group(1)) != 0
        ipset_names = checkout_netns_command(context, "ipset", "list", "-name").split()
        address, port_text = state["external_target"].rsplit(":", 1)
        expected_member = f"{address},tcp:{port_text}"
        orphaned_sets = [
            item["name"] for name in ipset_names
            for item in parse_ipset_blocks(
                checkout_netns_command(context, "ipset", "list", name)
            )
            if expected_member in item["members"]
        ]
        evidence = {"network_context": context, "active_netem": active_netem,
                    "chaos_output_hook_active": hook_active,
                    "classifier_chain_referenced": classifier_referenced,
                    "orphaned_inert_target_ipsets": orphaned_sets}
        return not active_netem and not hook_active and not classifier_referenced, evidence

    evidence = wait_until(cleaned, "F2 netem and target matcher cleanup", timeout=30)
    return {**evidence, "active_network_fault_state_removed": True}


def target_service_is_fully_unavailable(state: dict[str, int]) -> bool:
    return all(
        state[field] == 0
        for field in ("desired_replicas", "ready_replicas", "ready_endpoints", "pods")
    )


def wait_until(
    check: Callable[[], tuple[bool, Any]], description: str, timeout: float = 120
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        ready, last = check()
        if ready:
            return last
        time.sleep(1)
    raise FaultSmokeError(f"timed out waiting for {description}; last={last}")


def require_sut_ready() -> None:
    expected = {
        "adservice", "cartservice", "checkoutservice", "currencyservice",
        "emailservice", "frontend", "paymentservice", "productcatalogservice",
        "recommendationservice", "shippingservice", "redis-cart",
    }
    items = kube_json("-n", NAMESPACE, "get", "deployments", "-o", "json")["items"]
    actual = {item["metadata"]["name"] for item in items}
    if actual != expected:
        raise FaultSmokeError(f"SUT deployment mismatch: expected={expected}, actual={actual}")
    not_ready = {
        item["metadata"]["name"]: {
            "desired": item.get("spec", {}).get("replicas", 1),
            "ready": item.get("status", {}).get("readyReplicas", 0),
        }
        for item in items
        if item.get("status", {}).get("readyReplicas", 0)
        != item.get("spec", {}).get("replicas", 1)
    }
    if not_ready:
        raise FaultSmokeError(f"SUT is not fully Ready: {not_ready}")


def require_deployment_provenance() -> dict[str, Any]:
    output = Path(os.environ.get("TMPDIR", "/tmp")) / "tcc-fault-preflight-provenance.json"
    result = command(
        [sys.executable, str(PROVENANCE_VALIDATOR), "--output", str(output)],
        check=False,
    )
    try:
        evidence = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FaultSmokeError(
            f"deployment provenance validator returned invalid JSON: {result.stdout}"
        ) from exc
    if result.returncode or evidence.get("status") != "PASS":
        raise FaultSmokeError(f"deployment provenance hard blocker: {evidence}")
    return evidence


def chaos_items() -> list[dict[str, Any]]:
    result = kube(
        "get", "stresschaos,networkchaos", "--all-namespaces", "-o", "json", check=False
    )
    if result.returncode:
        if "the server doesn't have a resource type" in result.stderr:
            return []
        raise FaultSmokeError(result.stderr.strip())
    return json.loads(result.stdout).get("items", [])


def chaos_condition(item: dict[str, Any], condition: str) -> bool:
    return any(
        value.get("type") == condition
        and str(value.get("status", "")).lower() == "true"
        for value in item.get("status", {}).get("conditions", [])
    )


def resolve_service_cluster_ip_target(service: str, port: int) -> str:
    """Resolve a Service VIP just before injection; never fall back to Pod IPs."""
    item = kube_json("-n", NAMESPACE, "get", "service", service, "-o", "json")
    cluster_ip = item.get("spec", {}).get("clusterIP")
    if not isinstance(cluster_ip, str) or not cluster_ip or cluster_ip == "None":
        raise FaultSmokeError(f"Service {service} has no usable ClusterIP: {cluster_ip!r}")
    try:
        parsed = ipaddress.ip_address(cluster_ip)
    except ValueError as exc:
        raise FaultSmokeError(f"Service {service} has invalid ClusterIP: {cluster_ip!r}") from exc
    # Chaos Mesh 2.8.6's validated implementation path for this experiment uses
    # the IPv4 ipset/tc matcher demonstrated by the controlled Option A run.
    if parsed.version != 4:
        raise FaultSmokeError(f"Service {service} ClusterIP must be IPv4 for this F2 matcher")
    ports = item.get("spec", {}).get("ports") or []
    if not any(value.get("port") == port and value.get("protocol", "TCP") == "TCP" for value in ports):
        raise FaultSmokeError(f"Service {service} does not expose TCP port {port}")
    return f"{parsed}:{port}"


def chaos_manifest(
    fault: str, definition: dict[str, Any], *, f2_external_target: str | None = None
) -> dict[str, Any]:
    mechanism = definition["mechanism"]
    selector = {
        "namespaces": [NAMESPACE],
        "labelSelectors": {"app": "checkoutservice"},
    }
    if fault == "F1":
        kind = "StressChaos"
        spec = {
            "mode": "all",
            "selector": selector,
            "stressors": {
                "cpu": {
                    "workers": mechanism["workers"],
                    "load": mechanism["load_percent"],
                }
            },
            "duration": mechanism["failsafe_duration"],
        }
    elif fault == "F2":
        if not f2_external_target:
            raise FaultSmokeError("F2 requires a dynamically resolved Service ClusterIP target")
        kind = "NetworkChaos"
        spec = {
            "action": "delay",
            "mode": "all",
            "selector": selector,
            "direction": "to",
            "externalTargets": [f2_external_target],
            "delay": {
                "latency": f"{mechanism['latency_ms']}ms",
                "correlation": str(mechanism["correlation_percent"]),
                "jitter": f"{mechanism['jitter_ms']}ms",
            },
            "duration": mechanism["failsafe_duration"],
        }
    else:
        raise FaultSmokeError(f"{fault} is not a Chaos Mesh fault")
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": kind,
        "metadata": {
            "name": f"tcc-{fault.lower()}-stage4",
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/part-of": "tcc-experiment-v2"},
        },
        "spec": spec,
    }


def service_pods(service: str) -> list[dict[str, Any]]:
    return kube_json(
        "-n", NAMESPACE, "get", "pods", "-l", f"app={service}", "-o", "json"
    ).get("items") or []


def check_f4_hook_ready() -> list[dict[str, Any]]:
    pods = service_pods("paymentservice")
    if not pods:
        raise FaultSmokeError("paymentservice has no Pods")
    for pod in pods:
        name = pod["metadata"]["name"]
        result = kube(
            "-n", NAMESPACE, "exec", name, "-c", CONTAINER, "--", "sh", "-c",
            'test "${TCC_FAULT_INJECTION_ENABLED}" = true && '
            'test -d /tmp/tcc-faults && test -w /tmp/tcc-faults && '
            'test "${TCC_FAULT_FLAG_PATH}" = /tmp/tcc-faults/payment-charge-exception && '
            'test ! -e "$TCC_FAULT_FLAG_PATH"',
            check=False,
        )
        if result.returncode:
            raise FaultSmokeError(
                f"F4 hook is not armed safely in {name}: {result.stderr.strip()}"
            )
    return pods


def set_f4_flag(pods: list[dict[str, Any]], present: bool) -> dict[str, bool]:
    status: dict[str, bool] = {}
    for pod in pods:
        name = pod["metadata"]["name"]
        if present:
            kube(
                "-n", NAMESPACE, "exec", name, "-c", CONTAINER, "--", "sh", "-c",
                'umask 077; printf enabled > "$TCC_FAULT_FLAG_PATH"',
            )
        else:
            kube(
                "-n", NAMESPACE, "exec", name, "-c", CONTAINER, "--", "rm", "-f",
                "/tmp/tcc-faults/payment-charge-exception", check=False,
            )
        result = kube(
            "-n", NAMESPACE, "exec", name, "-c", CONTAINER, "--", "sh", "-c",
            'test -e "$TCC_FAULT_FLAG_PATH"', check=False,
        )
        status[name] = result.returncode == 0
    if any(value is not present for value in status.values()):
        raise FaultSmokeError(f"F4 flag state mismatch: {status}")
    return status


def start_port_forward(port: int) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [KUBECTL, "--context", CONTEXT, "-n", NAMESPACE,
         "port-forward", "service/frontend", f"{port}:80"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise FaultSmokeError("frontend port-forward exited before becoming ready")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/_healthz", timeout=1
            ):
                return process
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    process.terminate()
    raise FaultSmokeError("frontend port-forward did not become ready")


def run_workload(fault: str | None, port: int) -> dict[str, Any]:
    expected: list[str] = []
    if fault in {"F3", "F4"}:
        expected = ["POST:/cart/checkout=500"]
    elif fault == "F5":
        expected = [
            "GET:/=500",
            "GET:/product/OLJCESPC7Z=500",
            "POST:/cart=500",
            "GET:/cart=500",
            "POST:/cart/checkout=500",
        ]
    command_args = [
        sys.executable,
        str(WORKLOAD),
        "--base-url",
        f"http://127.0.0.1:{port}",
        "--warmup-flows",
        "0",
        "--steady-flows",
        "1",
        "--arrival-interval-seconds",
        "1",
        "--max-concurrent-flows",
        "1",
    ]
    for item in expected:
        command_args.extend(["--expect-status", item])
    result = command(command_args, check=False)
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FaultSmokeError(
            f"workload returned non-JSON output: {result.stdout}\n{result.stderr}"
        ) from exc
    if result.returncode:
        raise FaultSmokeError(f"fault workload probe failed: {summary}")
    return summary


def wait_for_normal_workload_recovery(
    port: int, timeout: float = 60
) -> dict[str, Any]:
    attempts = 0

    def probe() -> tuple[bool, Any]:
        nonlocal attempts
        attempts += 1
        try:
            summary = run_workload(None, port)
        except FaultSmokeError as exc:
            return False, {"attempts": attempts, "last_error": str(exc)}
        return True, {"attempts": attempts, "probe": summary}

    try:
        return wait_until(probe, "normal workload recovery", timeout=timeout)
    except FaultSmokeError as exc:
        raise FaultSmokeError(
            f"normal workload did not recover after {attempts} attempt(s): {exc}"
        ) from exc


def inject(fault: str, definition: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    mechanism = definition["mechanism"]
    if fault in {"F1", "F2"}:
        active = chaos_items()
        if active:
            raise FaultSmokeError("pre-existing Chaos Mesh resources must be absent")
        f2_target = None
        if fault == "F2":
            f2_target = resolve_service_cluster_ip_target("paymentservice", 50051)
        manifest = chaos_manifest(fault, definition, f2_external_target=f2_target)
        command(
            [KUBECTL, "--context", CONTEXT, "apply", "-f", "-"],
            input_text=json.dumps(manifest),
        )
        kind = manifest["kind"].lower()
        name = manifest["metadata"]["name"]
        namespace = manifest["metadata"]["namespace"]

        def injected() -> tuple[bool, Any]:
            item = kube_json("-n", namespace, "get", kind, name, "-o", "json")
            ready = chaos_condition(item, "Selected") and chaos_condition(item, "AllInjected")
            return ready, item.get("status", {})

        try:
            evidence = wait_until(
                injected, f"{fault} Chaos Mesh selection and injection"
            )
        except Exception:
            recover(fault, {"kind": kind, "name": name, "namespace": namespace})
            raise
        state = {"kind": kind, "name": name, "namespace": namespace}
        if fault == "F2":
            state["external_target"] = f2_target
            state["latency_ms"] = mechanism["latency_ms"]
        return state, {
            "control_plane_check": "Chaos Mesh Selected and AllInjected conditions",
            "evidence": json.dumps({"conditions": evidence, "external_target": f2_target}, sort_keys=True),
        }

    if fault in {"F3", "F5"}:
        name = mechanism["deployment"]
        before = deployment(name)
        replicas = desired_replicas(name)
        if replicas < 1 or ready_replicas(name) != replicas:
            raise FaultSmokeError(f"target deployment {name} is not at Ready baseline")
        if ready_endpoint_count(name) < 1:
            raise FaultSmokeError(f"target service {name} has no Ready endpoint")
        kube("-n", NAMESPACE, "scale", f"deployment/{name}", "--replicas=0")

        def unavailable() -> tuple[bool, Any]:
            state = {
                "desired_replicas": desired_replicas(name),
                "ready_replicas": ready_replicas(name),
                "ready_endpoints": ready_endpoint_count(name),
                "pods": len(service_pods(name)),
            }
            return target_service_is_fully_unavailable(state), state

        try:
            evidence = wait_until(
                unavailable, f"{name} Pods, replicas, and Service endpoints to reach zero"
            )
        except Exception:
            recover(fault, {"deployment": name, "replicas": replicas})
            raise
        return {"deployment": name, "replicas": replicas, "before": before}, {
            "control_plane_check": "Deployment replicas and ready EndpointSlice addresses are zero",
            "evidence": json.dumps(evidence, sort_keys=True),
        }

    if fault == "F4":
        pods = check_f4_hook_ready()
        pod_baseline = {
            pod["metadata"]["name"]: {
                "uid": pod["metadata"]["uid"],
                "restarts": pod["status"]["containerStatuses"][0]["restartCount"],
            }
            for pod in pods
        }
        try:
            flags = set_f4_flag(pods, True)
        except Exception:
            recover(fault, {"pods": pods, "baseline": pod_baseline})
            raise
        return {"pods": pods, "baseline": pod_baseline}, {
            "control_plane_check": "F4 flag exists in each Ready paymentservice Pod",
            "evidence": json.dumps(flags, sort_keys=True),
        }

    raise FaultSmokeError(f"unsupported fault {fault}")


def recover(fault: str, state: Any) -> dict[str, Any]:
    if fault in {"F1", "F2"}:
        kube(
            "-n", state["namespace"], "delete", state["kind"], state["name"],
            "--wait=true", "--timeout=120s",
        )

        def absent() -> tuple[bool, Any]:
            result = kube(
                "-n", state["namespace"], "get", state["kind"], state["name"],
                "-o", "json", check=False,
            )
            if result.returncode and "NotFound" in result.stderr:
                return True, {"resource_absent": True}
            if result.returncode:
                raise FaultSmokeError(result.stderr.strip())
            return False, {"resource_absent": False}

        result = wait_until(absent, "Chaos Mesh resource cleanup")
        if fault == "F2":
            result["network_recovery"] = verify_f2_network_recovered(state)
        return result

    if fault in {"F3", "F5"}:
        name = state["deployment"]
        kube(
            "-n", NAMESPACE, "scale", f"deployment/{name}",
            f"--replicas={state['replicas']}",
        )

        def restored() -> tuple[bool, Any]:
            data = {
                "desired_replicas": desired_replicas(name),
                "ready_replicas": ready_replicas(name),
                "ready_endpoints": ready_endpoint_count(name),
            }
            return (
                data["desired_replicas"] == state["replicas"]
                and data["ready_replicas"] == state["replicas"]
                and data["ready_endpoints"] >= 1,
                data,
            )

        return wait_until(restored, f"{name} replica and endpoint recovery")

    if fault == "F4":
        flags = set_f4_flag(state["pods"], False)
        current_pods = service_pods("paymentservice")
        current = {
            pod["metadata"]["name"]: {
                "uid": pod["metadata"]["uid"],
                "restarts": pod["status"]["containerStatuses"][0]["restartCount"],
            }
            for pod in current_pods
        }
        if current != state["baseline"]:
            raise FaultSmokeError(
                f"paymentservice Pod identity/restarts changed during F4: {current}"
            )
        return {"flags_absent_by_pod": flags, "pods_unchanged": current}

    raise FaultSmokeError(f"unsupported fault {fault}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault", required=True, choices=["F1", "F2", "F3", "F4", "F5"])
    parser.add_argument("--fault-id", help="physical repetition identity, e.g. F1-R01")
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--active-seconds", type=float, default=5.0)
    parser.add_argument("--frontend-port", type=int, default=18080)
    args = parser.parse_args()

    definitions = json.loads(FAULT_CONFIG.read_text(encoding="utf-8"))["faults"]
    fault_definition = definitions[args.fault]
    location = fault_definition["root_location"]
    record: GroundTruth | None = None
    report: dict[str, Any] = {
        "fault": args.fault,
        "started_at": utc_now(),
        "status": "FAIL",
        "control_plane": {},
    }
    state: Any = None
    injected = False
    port_forward: subprocess.Popen[str] | None = None
    try:
        require_exclusive_environment()
        report["deployment_provenance"] = require_deployment_provenance()
        require_sut_ready()
        port_forward = start_port_forward(args.frontend_port)
        report["normal_precondition_probe"] = run_workload(None, args.frontend_port)
        record = GroundTruth.request(
            args.fault_id or f"{args.fault}-stage4-smoke",
            fault_definition["fault_type"],
            location["kind"],
            location["value"],
            utc_now(),
        )
        state, confirmation = inject(args.fault, fault_definition)
        injected = True
        if args.fault == "F2":
            # This single ordinary checkout is control-plane confirmation only.
            # Start the model-visible fault window after its packet counters prove
            # that the exact ClusterIP:port matcher and netem processed traffic.
            before = f2_network_snapshot(state)
            confirmation_probe, network_confirmation = confirm_f2_network_effect(
                state, args.frontend_port, before
            )
            report["fault_confirmation_probe"] = confirmation_probe
            confirmation = {
                "control_plane_check": network_confirmation["control_plane_check"],
                "evidence": json.dumps({
                    "chaos_mesh": confirmation,
                    "network_effect": network_confirmation["evidence"],
                }, sort_keys=True),
            }
        record.confirm_injection(
            utc_now(), confirmation["control_plane_check"], confirmation["evidence"]
        )
        report["control_plane"]["injection"] = confirmation
        # Measure the configured active period from confirmed injection, not
        # from the earlier request. This keeps the Ground Truth window long
        # enough for downstream observations even when injection setup is slow.
        confirmed_active_started = time.monotonic()
        report["active_probe"] = run_workload(args.fault, args.frontend_port)
        remaining = args.active_seconds - (time.monotonic() - confirmed_active_started)
        if remaining > 0:
            time.sleep(remaining)
        recovery = recover(args.fault, state)
        injected = False
        record.confirm_recovery(
            utc_now(),
            "target restored and fault control absent",
            json.dumps(recovery, sort_keys=True),
        )
        report["control_plane"]["recovery"] = recovery
        post_recovery = wait_for_normal_workload_recovery(args.frontend_port)
        report["post_recovery_probe"] = post_recovery["probe"]
        report["post_recovery_probe_attempts"] = post_recovery["attempts"]
        report["status"] = "PASS"
    except Exception as exc:
        report["error"] = str(exc)
        if record is not None and record.status == "PENDING":
            record.mark_ineligible(f"fault smoke failed before confirmed injection: {exc}")
    finally:
        if injected and state is not None:
            try:
                cleanup = recover(args.fault, state)
                report["cleanup_recovery"] = cleanup
                if record is not None and record.status == "ACTIVE":
                    record.confirm_recovery(
                        utc_now(),
                        "cleanup restored target and removed fault control",
                        json.dumps(cleanup, sort_keys=True),
                    )
            except Exception as exc:
                report["cleanup_error"] = str(exc)
                report["status"] = "FAIL"
        if port_forward is not None:
            port_forward.terminate()
            try:
                port_forward.wait(timeout=5)
            except subprocess.TimeoutExpired:
                port_forward.kill()
                port_forward.wait()
        if record is not None:
            try:
                record.write_json(args.record)
            except (OSError, GroundTruthError) as exc:
                report["record_write_error"] = str(exc)
                report["status"] = "FAIL"

    report["finished_at"] = utc_now()
    report["ground_truth"] = record.to_dict() if record is not None else None
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
