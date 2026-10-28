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
    self.requests = meter.create_counter(
        "tcc.otel.rpc.server.requests", unit="{request}")
    self.errors = meter.create_counter(
        "tcc.otel.rpc.server.errors", unit="{error}")
    self.duration = meter.create_histogram(
        "tcc.otel.rpc.server.duration", unit="s")

  def record(self, operation, started_at, context, error):
    status = "OK"
    if error is not None:
      status = type(error).__name__
    elif context is not None and context.code() not in (None, grpc.StatusCode.OK):
      status = context.code().name
    attributes = {
        "rpc.system.name": "grpc",
        "rpc.method": operation,
        "rpc.response.status_code": status,
    }
    self.requests.add(1, attributes)
    if status != "OK":
      self.errors.add(1, attributes)
    self.duration.record(time.perf_counter() - started_at, attributes)


def configure_otel(service_name, application_logger):
  global _telemetry
  if os.getenv("ENABLE_TRACING") == "1" and _telemetry is None:
    _telemetry = OTelTelemetry(service_name, application_logger)
  return _telemetry


def otel_rpc(operation):
  def decorate(function):
    @functools.wraps(function)
    def invoke(*args, **kwargs):
      started_at = time.perf_counter()
      error = None
      try:
        return function(*args, **kwargs)
      except Exception as exception:
        error = exception
        raise
      finally:
        if _telemetry is not None:
          context = args[2] if len(args) > 2 else kwargs.get("context")
          _telemetry.record(operation, started_at, context, error)
    return invoke
  return decorate
