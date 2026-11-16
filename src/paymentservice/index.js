/*
 * Copyright 2018 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

'use strict';

let logger;

if (process.env.ENABLE_TRACING == "1") {
  const { resourceFromAttributes } = require('@opentelemetry/resources');

  const { ATTR_SERVICE_NAME }= require('@opentelemetry/semantic-conventions');

  const { GrpcInstrumentation } = require('@opentelemetry/instrumentation-grpc');
  const opentelemetry = require('@opentelemetry/sdk-node');

  const { OTLPTraceExporter } = require('@opentelemetry/exporter-otlp-grpc');
  const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-grpc');
  const { PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');
  const { OTLPLogExporter } = require('@opentelemetry/exporter-logs-otlp-grpc');
  const { BatchLogRecordProcessor } = require('@opentelemetry/sdk-logs');
  const { PinoInstrumentation } = require('@opentelemetry/instrumentation-pino');
  const { registerInstrumentations } = require('@opentelemetry/instrumentation');

  const collectorUrl = process.env.COLLECTOR_SERVICE_ADDR;
  const traceExporter = new OTLPTraceExporter({url: collectorUrl});
  const metricReader = new PeriodicExportingMetricReader({
    exporter: new OTLPMetricExporter({url: collectorUrl}),
    exportIntervalMillis: 5000
  });
  const logExporter = new OTLPLogExporter({ url: collectorUrl });
  const logRecordProcessor = new BatchLogRecordProcessor({ exporter: logExporter });

  const sdk = new opentelemetry.NodeSDK({
    resource: resourceFromAttributes({
      [ATTR_SERVICE_NAME]: process.env.OTEL_SERVICE_NAME || 'paymentservice',
      ...(process.env.POD_UID ? { 'service.instance.id': process.env.POD_UID } : {}),
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
} else {
  // Instrumentation is disabled for the conventional Scenario A image.
}

// Load the Pino singleton after instrumentation registration so one logger call
// is observed by both the conventional stdout destination and OTel Logs.
logger = require('./logger');
logger.info(process.env.ENABLE_TRACING == "1" ? "Tracing enabled." : "Tracing disabled.");

if (process.env.DISABLE_PROFILER) {
  logger.info("Profiler disabled.");
} else {
  logger.info("Profiler enabled.");
  require('@google-cloud/profiler').start({
    serviceContext: { service: 'paymentservice', version: '1.0.0' }
  });
}


const path = require('path');
const HipsterShopServer = require('./server');

const PORT = process.env['PORT'];
const PROTO_PATH = path.join(__dirname, '/proto/');

const server = new HipsterShopServer(PROTO_PATH, PORT);

server.listen();
