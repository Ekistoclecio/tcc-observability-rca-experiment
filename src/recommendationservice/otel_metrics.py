"""Native OpenTelemetry metrics for recommendationservice's gRPC boundary."""

import time

import grpc
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from logger import otel_resource


def configure_metrics(endpoint):
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=True),
        export_interval_millis=5000,
    )
    provider = MeterProvider(
        resource=otel_resource("recommendationservice"),
        metric_readers=[reader],
    )
    metrics.set_meter_provider(provider)
    return OTelServerMetrics()


class OTelServerMetrics:
    def __init__(self):
        meter = metrics.get_meter("recommendationservice.grpc")
        self.started = meter.create_counter("rpc.server.call.started", unit="{call}")
        self.duration = meter.create_histogram("rpc.server.call.duration", unit="s")

    def server_interceptor(self):
        owner = self

        class ServerMetricsInterceptor(grpc.ServerInterceptor):
            def intercept_service(self, continuation, details):
                handler = continuation(details)
                if handler is None or handler.unary_unary is None or details.method.startswith("/grpc.health."):
                    return handler

                def invoke(request, context):
                    started = time.perf_counter()
                    owner.started.add(1, {"rpc.system.name": "grpc", "rpc.method": details.method})
                    try:
                        return handler.unary_unary(request, context)
                    finally:
                        status = context.code()
                        status_code = str(status.value[0]) if status else "0"
                        owner.duration.record(
                            time.perf_counter() - started,
                            {
                                "rpc.system.name": "grpc",
                                "rpc.method": details.method,
                                "rpc.response.status_code": status_code,
                            },
                        )

                return grpc.unary_unary_rpc_method_handler(
                    invoke,
                    request_deserializer=handler.request_deserializer,
                    response_serializer=handler.response_serializer,
                )

        return ServerMetricsInterceptor()
