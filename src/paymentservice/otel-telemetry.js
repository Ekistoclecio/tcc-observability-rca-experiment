/*
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 */

'use strict';

const { metrics } = require('@opentelemetry/api');
const { logs, SeverityNumber } = require('@opentelemetry/api-logs');
const { OTLPLogExporter } = require('@opentelemetry/exporter-logs-otlp-grpc');
const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-grpc');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');
const { GrpcInstrumentation } = require('@opentelemetry/instrumentation-grpc');
const { PinoInstrumentation } = require('@opentelemetry/instrumentation-pino');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { BatchLogRecordProcessor } = require('@opentelemetry/sdk-logs');
const { PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');
const { NodeSDK } = require('@opentelemetry/sdk-node');
const {
  ATTR_DEPLOYMENT_ENVIRONMENT_NAME,
  ATTR_SERVICE_NAME,
  ATTR_SERVICE_VERSION
} = require('@opentelemetry/semantic-conventions');

let instance;

function createOTelTelemetry(defaultServiceName) {
  if (instance) return instance;
  const enabled = process.env.ENABLE_TRACING === '1';
  if (!enabled) {
    instance = {
      wrapUnary: (_operation, handler) => handler,
      emitLog: () => {},
      emitApplicationEvent: () => {},
      recordObservation: () => {}
    };
    return instance;
  }
  const serviceName = process.env.OTEL_SERVICE_NAME || defaultServiceName;
  const rawEndpoint = process.env.COLLECTOR_SERVICE_ADDR || 'localhost:4317';
  const endpoint = rawEndpoint.includes('://') ? rawEndpoint : `http://${rawEndpoint}`;
  const resource = resourceFromAttributes({
    [ATTR_SERVICE_NAME]: serviceName,
    [ATTR_SERVICE_VERSION]: '0.10.7',
    [ATTR_DEPLOYMENT_ENVIRONMENT_NAME]: 'tcc-local'
  });
  const sdk = new NodeSDK({
    resource,
    traceExporter: new OTLPTraceExporter({ url: endpoint }),
    metricReaders: [new PeriodicExportingMetricReader({
      exporter: new OTLPMetricExporter({ url: endpoint }),
      exportIntervalMillis: 5000
    })],
    logRecordProcessors: [new BatchLogRecordProcessor({
      exporter: new OTLPLogExporter({ url: endpoint })
    })],
    instrumentations: [
      new GrpcInstrumentation(),
      // The payment service's Pino records were not exported consistently by
      // instrumentation-pino in the async gRPC handler. Keep stdout isolated
      // and mirror the charge operation through the Logs API explicitly.
      new PinoInstrumentation({
        disableLogCorrelation: true,
        disableLogSending: defaultServiceName === 'paymentservice'
      })
    ]
  });
  sdk.start();

  const meter = metrics.getMeter(`online-boutique/${serviceName}`, '0.10.7');
  const requests = meter.createCounter('tcc.otel.rpc.server.requests', { unit: '{request}' });
  const errors = meter.createCounter('tcc.otel.rpc.server.errors', { unit: '{error}' });
  const duration = meter.createHistogram('tcc.otel.rpc.server.duration', { unit: 's' });
  const otelLogger = logs.getLogger(`online-boutique/${serviceName}`, '0.10.7');

  function emitLog(message, severityText = 'INFO') {
    const severityNumber = severityText === 'ERROR' ? SeverityNumber.ERROR : SeverityNumber.INFO;
    otelLogger.emit({ body: message, severityText, severityNumber });
  }

  function emitApplicationEvent(event) {
    const severityText = event.severity;
    const severityNumber = severityText === 'ERROR'
      ? SeverityNumber.ERROR
      : severityText === 'WARN' ? SeverityNumber.WARN : SeverityNumber.INFO;
    otelLogger.emit({
      body: event.message,
      severityText,
      severityNumber,
      attributes: {
        'observation.event': true,
        'event.name': event.eventName,
        'service.name': serviceName,
        'rpc.method': event.operation,
        ...(event.errorType ? { 'error.type': event.errorType } : {}),
        ...(event.errorMessage ? { 'error.message': event.errorMessage } : {})
      }
    });
  }

  // The application wrapper computes duration and outcome once. OTel receives
  // that observation instead of timing a second, nested wrapper.
  function recordObservation(observation) {
    const attributes = {
      'rpc.system.name': 'grpc',
      'rpc.method': observation.operation,
      'rpc.response.status_code': observation.statusCode,
      'kind': observation.kind,
      'outcome': observation.outcome
    };
    requests.add(1, attributes);
    if (observation.outcome === 'ERROR') errors.add(1, attributes);
    duration.record(observation.durationSeconds, attributes);
  }

  instance = { wrapUnary: (_operation, handler) => handler, emitLog, emitApplicationEvent, recordObservation };
  return instance;
}

module.exports = { createOTelTelemetry };
