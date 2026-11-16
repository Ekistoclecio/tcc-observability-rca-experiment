# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

import functools
import logging
import os
import time

import grpc
from conventional_telemetry import set_otel_observer
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_telemetry = None


class OTelTelemetry:
  def __init__(self, service_name, application_logger):
    endpoint = os.getenv("COLLECTOR_SERVICE_ADDR", "localhost:4317")
    resource = Resource.create({
        "service.name": service_name,
        "service.version": "0.10.7",
        "deployment.environment.name": "tcc-local",
    })
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(tracer_provider)
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=True),
        export_interval_millis=5000)
    metrics.set_meter_provider(MeterProvider(
        resource=resource, metric_readers=[metric_reader]))
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(
        OTLPLogExporter(endpoint=endpoint, insecure=True)))
    set_logger_provider(logger_provider)
    application_logger.addHandler(LoggingHandler(
        level=logging.NOTSET, logger_provider=logger_provider))
    meter = metrics.get_meter("online-boutique/" + service_name, "0.10.7")
    self.requests = meter.create_counter("tcc.otel.rpc.server.requests", unit="{request}")
    self.duration = meter.create_histogram("tcc.otel.rpc.server.duration", unit="s")
    self.client_requests = meter.create_counter("tcc.otel.rpc.client.requests", unit="{request}")
    self.client_duration = meter.create_histogram("tcc.otel.rpc.client.duration", unit="s")

  def record_observation(self, operation, duration_seconds, context, result, span_kind):
    status = result or "UNKNOWN"
    if context is not None and context.code() not in (None, grpc.StatusCode.OK):
      status = context.code().name
    outcome = "SUCCESS" if status == "OK" else "ERROR"
    attributes = {
        "rpc.system.name": "grpc",
        "rpc.method": operation,
        "rpc.response.status_code": status,
        "kind": span_kind.upper(),
        "outcome": outcome,
    }
    if span_kind == "client":
      self.client_requests.add(1, attributes)
      self.client_duration.record(duration_seconds, attributes)
    else:
      self.requests.add(1, attributes)
      self.duration.record(duration_seconds, attributes)


def configure_otel(service_name, application_logger):
  global _telemetry
  if os.getenv("ENABLE_TRACING") == "1" and _telemetry is None:
    _telemetry = OTelTelemetry(service_name, application_logger)
    set_otel_observer(_telemetry.record_observation)
  return _telemetry


def otel_rpc(operation):
  def decorate(function):
    # The gRPC interceptor owns the shared A/B request completion boundary.
    return function
  return decorate
