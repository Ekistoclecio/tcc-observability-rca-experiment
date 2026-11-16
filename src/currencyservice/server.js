/*
 * Copyright 2018 Google LLC.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

if(process.env.ENABLE_TRACING == "1") {
  const { GrpcInstrumentation } = require('@opentelemetry/instrumentation-grpc');
  const { registerInstrumentations } = require('@opentelemetry/instrumentation');
  const { PinoInstrumentation } = require('@opentelemetry/instrumentation-pino');

  const { resourceFromAttributes } = require('@opentelemetry/resources');

  const { ATTR_SERVICE_NAME } = require('@opentelemetry/semantic-conventions');

  const opentelemetry = require('@opentelemetry/sdk-node');

  const { OTLPTraceExporter } = require('@opentelemetry/exporter-otlp-grpc');
  const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-grpc');
  const { PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');
  const { OTLPLogExporter } = require('@opentelemetry/exporter-logs-otlp-grpc');
  const { BatchLogRecordProcessor } = require('@opentelemetry/sdk-logs');

  const collectorUrl = process.env.COLLECTOR_SERVICE_ADDR;
  const traceExporter = new OTLPTraceExporter({url: collectorUrl});
  const metricReader = new PeriodicExportingMetricReader({
    exporter: new OTLPMetricExporter({url: collectorUrl}),
    exportIntervalMillis: 5000
  });
  const logRecordProcessor = new BatchLogRecordProcessor({
    exporter: new OTLPLogExporter({ url: collectorUrl })
  });
  const sdk = new opentelemetry.NodeSDK({
    resource: resourceFromAttributes({
      [ ATTR_SERVICE_NAME ]: process.env.OTEL_SERVICE_NAME || 'currencyservice',
    }),
    traceExporter: traceExporter,
    metricReader: metricReader,
    logRecordProcessors: [logRecordProcessor],
  });

  registerInstrumentations({
    instrumentations: [
      new GrpcInstrumentation(),
      new PinoInstrumentation({ disableLogCorrelation: true }),
    ]
  });
  sdk.start()
}

// Pino must be loaded after its OTel instrumentation has been registered.
const pino = require('pino');
const logger = pino({
  name: 'currencyservice-server',
  messageKey: 'message',
  formatters: {
    level (logLevelString, logLevelNum) {
      return { severity: logLevelString }
    }
  }
});

logger.info(process.env.ENABLE_TRACING == "1" ? "Tracing enabled." : "Tracing disabled.");
if (process.env.DISABLE_PROFILER) {
  logger.info("Profiler disabled.");
} else {
  logger.info("Profiler enabled.");
  require('@google-cloud/profiler').start({
    serviceContext: { service: 'currencyservice', version: '1.0.0' }
  });
}

const path = require('path');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');
const { createConventionalTelemetry } = require('./conventional-telemetry');
const conventionalTelemetry = createConventionalTelemetry('currencyservice');
const { createOtelMetrics } = require('./otel-metrics');
const otelMetrics = createOtelMetrics('currencyservice');

const MAIN_PROTO_PATH = path.join(__dirname, './proto/demo.proto');
const HEALTH_PROTO_PATH = path.join(__dirname, './proto/grpc/health/v1/health.proto');

const PORT = process.env.PORT;

const shopProto = _loadProto(MAIN_PROTO_PATH).hipstershop;
const healthProto = _loadProto(HEALTH_PROTO_PATH).grpc.health.v1;

/**
 * Helper function that loads a protobuf file.
 */
function _loadProto (path) {
  const packageDefinition = protoLoader.loadSync(
    path,
    {
      keepCase: true,
      longs: String,
      enums: String,
      defaults: true,
      oneofs: true
    }
  );
  return grpc.loadPackageDefinition(packageDefinition);
}

/**
 * Helper function that gets currency data from a stored JSON file
 * Uses public data from European Central Bank
 */
function _getCurrencyData (callback) {
  const data = require('./data/currency_conversion.json');
  callback(data);
}

/**
 * Helper function that handles decimal/fractional carrying
 */
function _carry (amount) {
  const fractionSize = Math.pow(10, 9);
  amount.nanos += (amount.units % 1) * fractionSize;
  amount.units = Math.floor(amount.units) + Math.floor(amount.nanos / fractionSize);
  amount.nanos = amount.nanos % fractionSize;
  return amount;
}

/**
 * Lists the supported currencies
 */
function getSupportedCurrencies (call, callback) {
  logger.info('Getting supported currencies...');
  _getCurrencyData((data) => {
    callback(null, {currency_codes: Object.keys(data)});
  });
}

/**
 * Converts between currencies
 */
function convert (call, callback) {
  try {
    _getCurrencyData((data) => {
      const request = call.request;

      // Convert: from_currency --> EUR
      const from = request.from;
      const euros = _carry({
        units: from.units / data[from.currency_code],
        nanos: from.nanos / data[from.currency_code]
      });

      euros.nanos = Math.round(euros.nanos);

      // Convert: EUR --> to_currency
      const result = _carry({
        units: euros.units * data[request.to_code],
        nanos: euros.nanos * data[request.to_code]
      });

      result.units = Math.floor(result.units);
      result.nanos = Math.floor(result.nanos);
      result.currency_code = request.to_code;

      logger.info(`conversion request successful`);
      callback(null, result);
    });
  } catch (err) {
    logger.error(`conversion request failed: ${err}`);
    callback(err.message);
  }
}

/**
 * Endpoint for health checks
 */
function check (call, callback) {
  callback(null, { status: 'SERVING' });
}

/**
 * Starts an RPC server that receives requests for the
 * CurrencyConverter service at the sample server port
 */
function main () {
  logger.info(`Starting gRPC server on port ${PORT}...`);
  const server = new grpc.Server();
  server.addService(shopProto.CurrencyService.service, {
    getSupportedCurrencies: otelMetrics.wrapUnary(
      '/hipstershop.CurrencyService/GetSupportedCurrencies', conventionalTelemetry.wrapUnary(
        '/hipstershop.CurrencyService/GetSupportedCurrencies', getSupportedCurrencies)),
    convert: otelMetrics.wrapUnary(
      '/hipstershop.CurrencyService/Convert', conventionalTelemetry.wrapUnary(
        '/hipstershop.CurrencyService/Convert', convert))
  });
  server.addService(healthProto.Health.service, {check});

  server.bindAsync(
    `[::]:${PORT}`,
    grpc.ServerCredentials.createInsecure(),
    function() {
      logger.info(`CurrencyService gRPC server started on port ${PORT}`);
      server.start();
    },
   );
}

main();
