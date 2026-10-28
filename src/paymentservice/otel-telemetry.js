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
    instance = { wrapUnary: (_operation, handler) => handler };
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
      new PinoInstrumentation({ disableLogCorrelation: true, disableLogSending: false })
    ]
  });
  sdk.start();

  const meter = metrics.getMeter(`online-boutique/${serviceName}`, '0.10.7');
  const requests = meter.createCounter('tcc.otel.rpc.server.requests', { unit: '{request}' });
  const errors = meter.createCounter('tcc.otel.rpc.server.errors', { unit: '{error}' });
  const duration = meter.createHistogram('tcc.otel.rpc.server.duration', { unit: 's' });

  function wrapUnary(operation, handler) {
    return function otelMetricHandler(call, callback) {
      const startedAt = process.hrtime.bigint();
      let completed = false;
      function finish(error, response, trailer, flags) {
        if (completed) return;
        completed = true;
        const status = error ? String(error.code || error.name || 'ERROR') : 'OK';
        const attributes = {
          'rpc.system.name': 'grpc',
          'rpc.method': operation,
          'rpc.response.status_code': status
        };
        requests.add(1, attributes);
        if (error) errors.add(1, attributes);
        duration.record(Number(process.hrtime.bigint() - startedAt) / 1e9, attributes);
        callback(error, response, trailer, flags);
      }
      try {
        handler(call, finish);
      } catch (error) {
        finish(error);
      }
    };
  }
  instance = { wrapUnary };
  return instance;
}

module.exports = { createOTelTelemetry };
