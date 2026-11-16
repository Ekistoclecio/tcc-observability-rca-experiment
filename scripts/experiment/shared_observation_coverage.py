#!/usr/bin/env python3
"""Validate and publish the declared static Shared Observation coverage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "config/experiment/observation-contract.json"
REQUIRED_SERVICES = (
    "frontend", "checkoutservice", "cartservice", "productcatalogservice",
    "currencyservice", "paymentservice", "shippingservice", "emailservice",
    "recommendationservice", "adservice",
)
REQUIRED_MARKERS: dict[str, tuple[tuple[str, str], ...]] = {
    "frontend": (("src/frontend/conventional.go", "recordSharedHTTPServerObservation"),
                 ("src/frontend/conventional.go", "recordSharedRPCClientObservation"),
                 ("src/frontend/otel_telemetry.go", "otelHTTPDuration.Record(ctx, duration.Seconds(), opts)")),
    "checkoutservice": (("src/checkoutservice/conventional.go", "recordSharedRPCObservation(ctx"),
                         ("src/checkoutservice/otel_telemetry.go", "sharedRPCClientDuration.Record(ctx, duration.Seconds(), options)")),
    "cartservice": (("src/cartservice/src/ConventionalTelemetry.cs", "CartTelemetry.RecordRpcObservation"),
                    ("src/cartservice/src/cartstore/RedisCartStore.cs", "CompleteRedisObservation"),
                    ("src/cartservice/src/Telemetry.cs", "EmitApplicationEvent")),
    "productcatalogservice": (("src/productcatalogservice/conventional.go", "recordSharedRPCServerObservation"),
                              ("src/productcatalogservice/otel_telemetry.go", "sharedServerDuration.Record(ctx, duration.Seconds(), attrs)")),
    "currencyservice": (("src/currencyservice/conventional-telemetry.js", "dispatchObservation"),
                        ("src/currencyservice/otel-telemetry.js", "recordObservation")),
    "paymentservice": (("src/paymentservice/conventional-telemetry.js", "dispatchObservation"),
                       ("src/paymentservice/server.js", "dispatchApplicationEvent"),
                       ("src/paymentservice/otel-telemetry.js", "recordObservation")),
    "shippingservice": (("src/shippingservice/conventional.go", "recordSharedRPCServerObservation"),
                        ("src/shippingservice/otel_telemetry.go", "sharedServerDuration.Record(ctx, duration.Seconds(), attrs)")),
    "emailservice": (("src/emailservice/conventional_telemetry.py", "_OTEL_OBSERVER(method, duration_seconds, context, result, \"server\")"),
                     ("src/emailservice/otel_telemetry.py", "set_otel_observer(_telemetry.record_observation)")),
    "recommendationservice": (("src/recommendationservice/conventional_telemetry.py", "_OTEL_OBSERVER(client_call_details.method, duration_seconds, None, result, \"client\")"),
                              ("src/recommendationservice/otel_telemetry.py", "set_otel_observer(_telemetry.record_observation)")),
    "adservice": (("src/adservice/src/main/java/hipstershop/ConventionalTelemetry.java", "sharedRequests.add(1, attributes)"),
                  ("src/adservice/src/main/java/hipstershop/ConventionalTelemetry.java", "sharedDuration.record(durationSeconds, attributes)")),
}


def audit(contract: dict[str, Any]) -> dict[str, Any]:
    declared = contract.get("service_coverage", {})
    issues: list[str] = []
    services: list[dict[str, Any]] = []
    if set(declared) != set(REQUIRED_SERVICES):
        issues.append("The service coverage keys do not match the ten required application services.")
    for service in REQUIRED_SERVICES:
        row = declared.get(service, {})
        if not row:
            issues.append(f"{service}: no coverage declaration")
            continue
        status = row.get("status")
        if status not in {"COMPLETE", "COMPLETE_WITH_NA"}:
            issues.append(f"{service}: status is {status!r}")
        if set(row.get("shared_metric_adapters", [])) != {"conventional", "opentelemetry"}:
            issues.append(f"{service}: both shared metric adapters are not declared")
        na_reasons = row.get("na_reasons", {})
        for field in ("server_request", "client_downstream", "datastore", "application_events"):
            if field not in row or not isinstance(row[field], bool):
                issues.append(f"{service}: {field} is not explicitly classified")
            elif row[field] is False and not na_reasons.get(field):
                issues.append(f"{service}: {field}=N/A has no reason")
        evidence = row.get("evidence", [])
        if not evidence:
            issues.append(f"{service}: no source evidence paths")
        missing_paths = [path for path in evidence if not (REPO / path).is_file()]
        if missing_paths:
            issues.append(f"{service}: evidence paths are missing: {missing_paths}")
        marker_results = []
        for path, marker in REQUIRED_MARKERS[service]:
            source_path = REPO / path
            found = source_path.is_file() and marker in source_path.read_text(encoding="utf-8")
            marker_results.append({"path": path, "marker": marker, "found": found})
            if not found:
                issues.append(f"{service}: source marker not found: {path}: {marker}")
        services.append({
            "service": service,
            "status": status,
            "server_request": row.get("server_request"),
            "client_downstream": row.get("client_downstream"),
            "datastore": row.get("datastore"),
            "application_events": row.get("application_events"),
            "request_count": set(row.get("shared_metric_adapters", [])) == {"conventional", "opentelemetry"},
            "request_duration": set(row.get("shared_metric_adapters", [])) == {"conventional", "opentelemetry"},
            "metric_adapters": row.get("shared_metric_adapters", []),
            "na_reasons": na_reasons,
            "evidence": evidence,
            "source_markers": marker_results,
        })
    return {
        "schema_version": "1.0",
        "result": "PASS" if not issues else "FAIL",
        "fault_execution_allowed": not issues,
        "service_count": len(services),
        "complete": sum(item["status"] == "COMPLETE" for item in services),
        "complete_with_na": sum(item["status"] == "COMPLETE_WITH_NA" for item in services),
        "partial": sum(item["status"] not in {"COMPLETE", "COMPLETE_WITH_NA"} for item in services),
        "services": services,
        "issues": issues,
        "source_parity_of_existing_raw": "NOT_APPLICABLE_OLD_INSTRUMENTATION",
    }


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "shared-observation-coverage.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# Cobertura estática do Shared Observation Contract", "",
        f"Resultado: **{report['result']}**. Execução de faults permitida: **{report['fault_execution_allowed']}**.", "",
        "| Serviço | Estado | SERVER | CLIENT | DATASTORE | APPLICATION_EVENT | request_count | request_duration | Adaptadores A/B |", "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for service in report["services"]:
        lines.append("| `{service}` | **{status}** | {server_request} | {client_downstream} | {datastore} | {application_events} | {adapters} |".format(
            service=service["service"], status=service["status"],
            server_request="sim" if service["server_request"] else "N/A",
            client_downstream="sim" if service["client_downstream"] else "N/A",
            datastore="sim" if service["datastore"] else "N/A",
            application_events="sim" if service["application_events"] else "N/A",
            request_count="sim" if service["request_count"] else "não",
            request_duration="sim" if service["request_duration"] else "não",
            adapters=", ".join(service["metric_adapters"])))
    lines += ["", "## Evidência e N/A", ""]
    for service in report["services"]:
        lines.append(f"### {service['service']}")
        lines.append("")
        lines.append("Fontes: " + ", ".join(f"`{path}`" for path in service["evidence"]) + ".")
        if service["na_reasons"]:
            lines.append("N/A: " + "; ".join(f"`{key}` — {value}" for key, value in service["na_reasons"].items()) + ".")
        lines.append("Source markers: " + ("todos encontrados" if all(m["found"] for m in service["source_markers"]) else "há markers ausentes") + ".")
        lines.append("")
    if report["issues"]:
        lines += ["## Bloqueios", ""] + [f"- {issue}" for issue in report["issues"]]
    lines += ["", "RAWs históricos não foram usados para validar source parity: `NOT_APPLICABLE_OLD_INSTRUMENTATION`.", ""]
    (output_dir / "shared-observation-coverage.md").write_text("\n".join(lines), encoding="utf-8")
    (output_dir / "final-service-coverage.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=CONTRACT)
    parser.add_argument("--output", type=Path, default=REPO / "audit/observability-contract")
    args = parser.parse_args()
    report = audit(json.loads(args.contract.read_text(encoding="utf-8")))
    write_report(report, args.output)
    print(json.dumps({key: report[key] for key in ("result", "fault_execution_allowed", "complete", "complete_with_na", "partial", "issues")}, indent=2, ensure_ascii=False))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
