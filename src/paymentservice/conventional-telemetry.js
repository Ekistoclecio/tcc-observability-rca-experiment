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

const { AsyncLocalStorage } = require('async_hooks');
const crypto = require('crypto');
const http = require('http');
const { dispatchObservation } = require('./shared-observation');

const TRACE_HEADER = 'x-tcc-trace-id';
const SPAN_HEADER = 'x-tcc-span-id';

function newId(bytes) {
  return crypto.randomBytes(bytes).toString('hex');
}

function metadataValue(metadata, key) {
  const values = metadata && metadata.get(key);
  return values && values.length > 0 ? String(values[0]) : undefined;
}

function extractContext(metadata) {
  return {
    traceId: metadataValue(metadata, TRACE_HEADER),
    parentSpanId: metadataValue(metadata, SPAN_HEADER)
  };
}

function escapeLabel(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');
}

function createConventionalTelemetry(serviceName) {
  const enabled = process.env.ENABLE_CONVENTIONAL_TELEMETRY === '1';
  const zipkinEndpoint = new URL(process.env.CONVENTIONAL_ZIPKIN_ENDPOINT ||
    'http://jaeger-conventional.observability-conventional.svc.cluster.local:9411/api/v2/spans');
  const storage = new AsyncLocalStorage();
  const metrics = new Map();

  function record(operation, result, durationSeconds) {
    const key = JSON.stringify([operation, result]);
    const current = metrics.get(key) || { count: 0, duration: 0 };
    current.count += 1;
    current.duration += durationSeconds;
    metrics.set(key, current);
  }

  function renderMetrics() {
    const lines = [
      '# HELP tcc_conventional_requests_total Completed conventional requests and RPCs.',
      '# TYPE tcc_conventional_requests_total counter'
    ];
    for (const [key, value] of [...metrics.entries()].sort()) {
      const [operation, result] = JSON.parse(key);
      const labels = `service_name="${escapeLabel(serviceName)}",operation="${escapeLabel(operation)}",span_kind="server",status_code="${escapeLabel(result)}"`;
      lines.push(`tcc_conventional_requests_total{${labels}} ${value.count}`);
      lines.push(`tcc_conventional_request_duration_seconds_sum{${labels}} ${value.duration}`);
      lines.push(`tcc_conventional_request_duration_seconds_count{${labels}} ${value.count}`);
    }
    return `${lines.join('\n')}\n`;
  }

  function exportSpan(span) {
    const body = JSON.stringify([span]);
    const request = http.request(zipkinEndpoint, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(body)
      },
      timeout: 2000
    }, response => response.resume());
    request.on('error', () => {});
    request.on('timeout', () => request.destroy());
    request.end(body);
  }

  function recordObservation(observation) {
    if (!enabled) return;
    record(observation.operation, observation.statusCode, observation.durationSeconds);
  }

  function emitApplicationEvent(event) {
    if (!enabled) return;
    // Conventional logs intentionally do not receive trace/span identifiers.
    process.stdout.write(`${JSON.stringify({
      timestamp: new Date().toISOString(),
      service: serviceName,
      severity: event.severity,
      observation_event: true,
      event_name: event.eventName,
      operation: event.operation,
      error_type: event.errorType,
      error_message: event.errorMessage,
      message: event.message
    })}\n`);
  }

  function wrapUnary(operation, handler, observe = null) {
    if (!enabled && typeof observe !== 'function') return handler;
    if (!enabled) {
      return function sharedMeasurementHandler(call, callback) {
        const startedAtMicros = Date.now() * 1000;
        const startedAt = process.hrtime.bigint();
        let completed = false;
        function finish(error, response, trailer, flags) {
          if (completed) return;
          completed = true;
          const durationSeconds = Number(process.hrtime.bigint() - startedAt) / 1e9;
          dispatchObservation({
            service: serviceName,
            operation,
            kind: 'SERVER',
            statusCode: error ? String(error.code || error.name || 'ERROR') : 'OK',
            outcome: error ? 'ERROR' : 'SUCCESS',
            durationSeconds,
            startedAtMicros
          }, [{ recordObservation: observe }]);
          callback(error, response, trailer, flags);
        }
        try {
          handler(call, finish);
        } catch (error) {
          finish(error);
        }
      };
    }
    return function conventionalUnaryHandler(call, callback) {
      const incoming = extractContext(call.metadata);
      const traceId = incoming.traceId || newId(16);
      const spanId = newId(8);
      const startedAtMicros = Date.now() * 1000;
      const startedAt = process.hrtime.bigint();
      let completed = false;

      function finish(error, response, trailer, flags) {
        if (completed) return;
        completed = true;
        const durationMicros = Number((process.hrtime.bigint() - startedAt) / 1000n);
        const result = error ? String(error.code || error.name || 'ERROR') : 'OK';
        const observation = {
          service: serviceName,
          operation,
          kind: 'SERVER',
          statusCode: result,
          outcome: error ? 'ERROR' : 'SUCCESS',
          durationSeconds: durationMicros / 1e6,
          startedAtMicros
        };
        dispatchObservation(observation, [
          { recordObservation },
          ...(observe ? [{ recordObservation: observe }] : [])
        ]);
        const span = {
          traceId,
          id: spanId,
          name: operation,
          kind: 'SERVER',
          timestamp: startedAtMicros,
          duration: Math.max(1, durationMicros),
          localEndpoint: { serviceName },
          tags: {
            'telemetry.scenario': 'conventional',
            'service.version': '0.10.7',
            result
          }
        };
        if (incoming.parentSpanId) span.parentId = incoming.parentSpanId;
        exportSpan(span);
        callback(error, response, trailer, flags);
      }

      storage.run({ traceId, spanId }, () => {
        try {
          handler(call, finish);
        } catch (error) {
          finish(error);
        }
      });
    };
  }

  if (enabled) {
    const port = Number(process.env.CONVENTIONAL_METRICS_PORT || 9464);
    const server = http.createServer((request, response) => {
      const body = renderMetrics();
      response.writeHead(200, {
        'content-type': 'text/plain; version=0.0.4; charset=utf-8',
        'content-length': Buffer.byteLength(body)
      });
      response.end(body);
    });
    server.listen(port, '0.0.0.0');
  }

  return { enabled, wrapUnary, recordObservation, emitApplicationEvent, renderMetrics, currentContext: () => storage.getStore() };
}

module.exports = { TRACE_HEADER, SPAN_HEADER, createConventionalTelemetry, extractContext };
