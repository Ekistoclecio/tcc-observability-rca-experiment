# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Independent conventional tracing and metrics for Python gRPC services."""

from collections import defaultdict, namedtuple
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import threading
import time
from urllib import request as urllib_request

import grpc


TRACE_HEADER = "x-tcc-trace-id"
SPAN_HEADER = "x-tcc-span-id"
_CURRENT_CONTEXT = ContextVar("tcc_conventional_context", default=None)
_OTEL_OBSERVER = None


def set_otel_observer(observer):
    """Register the B adapter used by the shared completion boundary."""
    global _OTEL_OBSERVER
    _OTEL_OBSERVER = observer


def extract_context(metadata):
    """Extract only the experiment's conventional headers, never W3C context."""
    values = {item.key.lower(): item.value for item in (metadata or ())}
    return values.get(TRACE_HEADER), values.get(SPAN_HEADER)


class _ClientCallDetails(
        namedtuple("_ClientCallDetails", (
            "method", "timeout", "metadata", "credentials",
            "wait_for_ready", "compression")), grpc.ClientCallDetails):
    pass


class ConventionalTelemetry:
    def __init__(self, service_name):
        self.service_name = service_name
        self.enabled = os.getenv("ENABLE_CONVENTIONAL_TELEMETRY") == "1"
        self.zipkin_endpoint = os.getenv(
            "CONVENTIONAL_ZIPKIN_ENDPOINT",
            "http://jaeger-conventional.observability-conventional.svc.cluster.local:9411/api/v2/spans")
        self._metrics = defaultdict(lambda: [0, 0.0])
        self._metrics_lock = threading.Lock()
        if self.enabled:
            self._start_metrics_server(int(os.getenv("CONVENTIONAL_METRICS_PORT", "9464")))

    @staticmethod
    def _new_id(byte_count):
        return secrets.token_hex(byte_count)

    def _record(self, operation, span_kind, result, duration_seconds):
        with self._metrics_lock:
            metric = self._metrics[(operation, span_kind, result)]
            metric[0] += 1
            metric[1] += duration_seconds

    @staticmethod
    def _escape_label(value):
        return str(value).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")

    def render_metrics(self):
        lines = [
            "# HELP tcc_conventional_requests_total Completed conventional requests and RPCs.",
            "# TYPE tcc_conventional_requests_total counter",
        ]
        with self._metrics_lock:
            items = sorted(self._metrics.items())
        for (operation, span_kind, result), (count, duration) in items:
            labels = (
                f'service_name="{self._escape_label(self.service_name)}",'
                f'operation="{self._escape_label(operation)}",'
                f'span_kind="{span_kind}",status_code="{self._escape_label(result)}"')
            lines.append(f"tcc_conventional_requests_total{{{labels}}} {count}")
            lines.append(f"tcc_conventional_request_duration_seconds_sum{{{labels}}} {duration}")
            lines.append(f"tcc_conventional_request_duration_seconds_count{{{labels}}} {count}")
        return "\n".join(lines) + "\n"

    def _export(self, span):
        def send():
            try:
                body = json.dumps([span], separators=(",", ":")).encode("utf-8")
                req = urllib_request.Request(
                    self.zipkin_endpoint, data=body,
                    headers={"content-type": "application/json"}, method="POST")
                with urllib_request.urlopen(req, timeout=2):
                    pass
            except Exception:
                # Telemetry export must never change application behavior.
                pass

        threading.Thread(target=send, daemon=True).start()

    def _complete_span(self, trace_id, span_id, parent_id, operation,
                       span_kind, result, started_at_micros, started_at_ns,
                       duration_seconds=None):
        if duration_seconds is None:
            duration_seconds = max(0.0, (time.perf_counter_ns() - started_at_ns) / 1e9)
        duration_micros = max(1, int(duration_seconds * 1e6))
        self._record(operation, span_kind.lower(), result, duration_micros / 1_000_000)
        span = {
            "traceId": trace_id,
            "id": span_id,
            "name": operation,
            "kind": span_kind,
            "timestamp": started_at_micros,
            "duration": duration_micros,
            "localEndpoint": {"serviceName": self.service_name},
            "tags": {
                "telemetry.scenario": "conventional",
                "service.version": "0.10.7",
                "result": result,
            },
        }
        if parent_id:
            span["parentId"] = parent_id
        self._export(span)

    def server_interceptor(self):
        telemetry = self

        class ServerInterceptor(grpc.ServerInterceptor):
            def intercept_service(self, continuation, handler_call_details):
                handler = continuation(handler_call_details)
                method = handler_call_details.method
                if (handler is None or
                        handler.unary_unary is None or method.startswith("/grpc.health.")):
                    return handler

                def invoke(request, context):
                    trace_id, parent_id, span_id = "", "", ""
                    if telemetry.enabled:
                        trace_id, parent_id = extract_context(handler_call_details.invocation_metadata)
                        trace_id = trace_id or telemetry._new_id(16)
                        span_id = telemetry._new_id(8)
                    started_at_micros = time.time_ns() // 1000
                    started_at_ns = time.perf_counter_ns()
                    token = _CURRENT_CONTEXT.set((trace_id, span_id)) if telemetry.enabled else None
                    result = "OK"
                    try:
                        return handler.unary_unary(request, context)
                    except Exception as error:
                        result = type(error).__name__
                        raise
                    finally:
                        if context.code() not in (None, grpc.StatusCode.OK):
                            result = context.code().name
                        if token is not None:
                            _CURRENT_CONTEXT.reset(token)
                        duration_seconds = max(0.0, (time.perf_counter_ns() - started_at_ns) / 1e9)
                        if telemetry.enabled:
                            telemetry._complete_span(
                                trace_id, span_id, parent_id, method, "SERVER", result,
                                started_at_micros, started_at_ns, duration_seconds)
                        if _OTEL_OBSERVER is not None:
                            _OTEL_OBSERVER(method, duration_seconds, context, result, "server")

                return grpc.unary_unary_rpc_method_handler(
                    invoke,
                    request_deserializer=handler.request_deserializer,
                    response_serializer=handler.response_serializer)

        return ServerInterceptor()

    def _start_metrics_server(self, port):
        telemetry = self

        class MetricsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = telemetry.render_metrics().encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "text/plain; version=0.0.4")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                return

        server = ThreadingHTTPServer(("0.0.0.0", port), MetricsHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

