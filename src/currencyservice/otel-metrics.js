'use strict';

const { metrics } = require('@opentelemetry/api');

function createOtelMetrics(serviceName) {
  const duration = metrics.getMeter(`${serviceName}.grpc`).createHistogram(
    'rpc.server.call.duration', { unit: 's' });

  function wrapUnary(operation, handler) {
    return function otelUnaryHandler(call, callback) {
      const started = process.hrtime.bigint();
      let completed = false;

      function finish(error, response, trailer, flags) {
        if (completed) return;
        completed = true;
        const durationSeconds = Number(process.hrtime.bigint() - started) / 1e9;
        const statusCode = error ? String(error.code ?? 'ERROR') : '0';
        duration.record(durationSeconds, {
          'rpc.system.name': 'grpc',
          'rpc.method': operation,
          'rpc.response.status_code': statusCode
        });
        callback(error, response, trailer, flags);
      }

      try {
        handler(call, finish);
      } catch (error) {
        finish(error);
      }
    };
  }

  return { wrapUnary };
}

module.exports = { createOtelMetrics };
