#!/usr/bin/env node

const config = {
  startMicros: Number(process.env.CHECKOUT_START_US),
  endMicros: Number(process.env.CHECKOUT_END_US),
  jaegerA: process.env.JAEGER_A_URL || 'http://127.0.0.1:26686',
  jaegerB: process.env.JAEGER_B_URL || 'http://127.0.0.1:27686',
  prometheusA: process.env.PROMETHEUS_A_URL || 'http://127.0.0.1:29091',
  prometheusB: process.env.PROMETHEUS_B_URL || 'http://127.0.0.1:29092',
  lokiA: process.env.LOKI_A_URL || 'http://127.0.0.1:23100',
  lokiB: process.env.LOKI_B_URL || 'http://127.0.0.1:23101'
};

if (!Number.isFinite(config.startMicros) || !Number.isFinite(config.endMicros)) {
  throw new Error('CHECKOUT_START_US and CHECKOUT_END_US are required');
}

const requiredCheckoutServices = [
  'frontend',
  'checkoutservice',
  'cartservice',
  'productcatalogservice',
  'currencyservice',
  'paymentservice',
  'shippingservice',
  'emailservice',
  'recommendationservice'
];

const applicationServices = [
  'frontend',
  'checkoutservice',
  'cartservice',
  'productcatalogservice',
  'currencyservice',
  'paymentservice',
  'shippingservice',
  'emailservice',
  'recommendationservice',
  'adservice'
];

const directMetricPatterns = {
  frontend: 'online_boutique_otel_http_server_request_duration_seconds_count',
  checkoutservice: 'online_boutique_otel_rpc_server_call_duration_seconds_count',
  cartservice: 'online_boutique_otel_tcc_otel_rpc_server_requests_total',
  productcatalogservice: 'online_boutique_otel_rpc_server_call_duration_seconds_count',
  currencyservice: 'online_boutique_otel_tcc_otel_rpc_server_requests_total',
  paymentservice: 'online_boutique_otel_tcc_otel_rpc_server_requests_total',
  shippingservice: 'online_boutique_otel_rpc_server_call_duration_seconds_count',
  emailservice: 'online_boutique_otel_tcc_otel_rpc_server_requests_total',
  recommendationservice: 'online_boutique_otel_tcc_otel_rpc_server_requests_total',
  // The Java agent emits RPC server duration in milliseconds under its own
  // instrumentation scope; it is still produced directly by the OTel SDK.
  adservice: 'online_boutique_otel_rpc_server_duration_milliseconds_count'
};

const durationMetricPatterns = {
  frontend: 'online_boutique_otel_http_server_request_duration_seconds_count',
  checkoutservice: 'online_boutique_otel_rpc_server_call_duration_seconds_count',
  cartservice: 'online_boutique_otel_tcc_otel_rpc_server_duration_seconds_count',
  productcatalogservice: 'online_boutique_otel_rpc_server_call_duration_seconds_count',
  currencyservice: 'online_boutique_otel_tcc_otel_rpc_server_duration_seconds_count',
  paymentservice: 'online_boutique_otel_tcc_otel_rpc_server_duration_seconds_count',
  shippingservice: 'online_boutique_otel_rpc_server_call_duration_seconds_count',
  emailservice: 'online_boutique_otel_tcc_otel_rpc_server_duration_seconds_count',
  recommendationservice: 'online_boutique_otel_tcc_otel_rpc_server_duration_seconds_count',
  adservice: 'online_boutique_otel_rpc_server_duration_milliseconds_count'
};

function assert(check, message) {
  if (!check) throw new Error(message);
}

async function json(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} from ${url}`);
  return response.json();
}

function services(trace) {
  return [...new Set(Object.values(trace.processes || {}).map(process => process.serviceName))].sort();
}

function operationNames(trace, service) {
  return trace.spans
    .filter(span => trace.processes[span.processID]?.serviceName === service)
    .map(span => span.operationName);
}

async function findCheckoutTrace(baseUrl) {
  const url = new URL('/api/traces', baseUrl);
  url.searchParams.set('service', 'frontend');
  url.searchParams.set('start', String(config.startMicros - 2_000_000));
  url.searchParams.set('end', String(config.endMicros + 8_000_000));
  url.searchParams.set('limit', '100');

  for (let attempt = 0; attempt < 15; attempt += 1) {
    const response = await json(url);
    const candidates = (response.data || [])
      .filter(trace => {
        const observed = services(trace);
        return requiredCheckoutServices.every(service => observed.includes(service));
      })
      .sort((left, right) => right.spans.length - left.spans.length);
    if (candidates.length > 0) return candidates[0];
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error(`checkout trace not found in ${baseUrl}`);
}

async function traceCount(baseUrl, traceId) {
  const response = await fetch(new URL(`/api/traces/${traceId}`, baseUrl));
  if (response.status === 404) return 0;
  if (!response.ok) throw new Error(`${response.status} while querying trace ${traceId}`);
  const body = await response.json();
  return (body.data || []).length;
}

async function operationSpanCount(baseUrl, service, operation) {
  const url = new URL('/api/traces', baseUrl);
  url.searchParams.set('service', service);
  url.searchParams.set('operation', operation);
  url.searchParams.set('start', String(config.startMicros - 2_000_000));
  url.searchParams.set('end', String(config.endMicros + 12_000_000));
  url.searchParams.set('limit', '100');
  const response = await json(url);
  return (response.data || []).reduce(
    (total, trace) => total + trace.spans.filter(span => span.operationName === operation).length,
    0);
}

async function prometheusValue(baseUrl, query) {
  const url = new URL('/api/v1/query', baseUrl);
  url.searchParams.set('query', query);
  const response = await json(url);
  const result = response.data?.result || [];
  return result.length === 0 ? 0 : Number(result[0].value[1]);
}

async function waitForPrometheusValue(baseUrl, query) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const value = await prometheusValue(baseUrl, query);
    if (value > 0) return value;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  return 0;
}

async function prometheusExemplars(baseUrl, query) {
  const url = new URL('/api/v1/query_exemplars', baseUrl);
  url.searchParams.set('query', query);
  url.searchParams.set('start', String((config.startMicros - 5_000_000) / 1_000_000));
  url.searchParams.set('end', String((config.endMicros + 20_000_000) / 1_000_000));
  const response = await json(url);
  return (response.data || []).reduce(
    (total, series) => total + (series.exemplars || []).length, 0);
}

async function lokiMatches(baseUrl, traceId, service = '.+') {
  const url = new URL('/loki/api/v1/query_range', baseUrl);
  const selector = service === '.+' ? 'service_name=~".+"' : `service_name="${service}"`;
  url.searchParams.set('query', `{${selector}} | trace_id = "${traceId}"`);
  url.searchParams.set('start', String((config.startMicros - 5_000_000) * 1000));
  url.searchParams.set('end', String((config.endMicros + 10_000_000) * 1000));
  url.searchParams.set('limit', '100');
  const response = await json(url);
  return (response.data?.result || []).reduce((total, stream) => total + stream.values.length, 0);
}

async function correlatedLokiMatches(baseUrl, traceId, service) {
  const url = new URL('/loki/api/v1/query_range', baseUrl);
  url.searchParams.set('query', `{service_name="${service}"} | trace_id = "${traceId}" | span_id != "" | severity_text != ""`);
  url.searchParams.set('start', String((config.startMicros - 5_000_000) * 1000));
  url.searchParams.set('end', String((config.endMicros + 20_000_000) * 1000));
  url.searchParams.set('limit', '100');
  const response = await json(url);
  return (response.data?.result || []).reduce((total, stream) => total + stream.values.length, 0);
}

async function lokiQueryMatches(baseUrl, query) {
  const url = new URL('/loki/api/v1/query_range', baseUrl);
  url.searchParams.set('query', query);
  url.searchParams.set('start', String((config.startMicros - 5_000_000) * 1000));
  url.searchParams.set('end', String((config.endMicros + 20_000_000) * 1000));
  url.searchParams.set('limit', '100');
  const response = await json(url);
  return (response.data?.result || []).reduce((total, stream) => total + stream.values.length, 0);
}

async function serviceTraceIds(service) {
  const url = new URL('/api/traces', config.jaegerB);
  url.searchParams.set('service', service);
  url.searchParams.set('start', String(config.startMicros - 5_000_000));
  url.searchParams.set('end', String(config.endMicros + 20_000_000));
  url.searchParams.set('limit', '100');
  const response = await json(url);
  return [...new Set((response.data || []).map(trace => trace.traceID))];
}

async function waitForServiceLog(service) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    for (const traceId of await serviceTraceIds(service)) {
      const matches = await correlatedLokiMatches(config.lokiB, traceId, service);
      if (matches > 0) return { traceId, matches };
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  return { traceId: null, matches: 0 };
}

async function waitForLokiMatches(baseUrl, traceId) {
  for (let attempt = 0; attempt < 15; attempt += 1) {
    const matches = await lokiMatches(baseUrl, traceId);
    if (matches > 0) return matches;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  return 0;
}

const [traceA, traceB] = await Promise.all([
  findCheckoutTrace(config.jaegerA),
  findCheckoutTrace(config.jaegerB)
]);

const servicesA = services(traceA);
const servicesB = services(traceB);
const missingA = requiredCheckoutServices.filter(service => !servicesA.includes(service));
const missingB = requiredCheckoutServices.filter(service => !servicesB.includes(service));
const cartOperationsA = operationNames(traceA, 'cartservice');
const cartOperationsB = operationNames(traceB, 'cartservice');
const startA = Math.min(...traceA.spans.map(span => span.startTime));
const startB = Math.min(...traceB.spans.map(span => span.startTime));

await new Promise(resolve => setTimeout(resolve, 2500));

const [aInB, bInA, conventionalA, conventionalB, otelA, otelB,
  frontendExporterSpans, checkoutExporterSpans, catalogExporterSpans] =
  await Promise.all([
    traceCount(config.jaegerB, traceA.traceID),
    traceCount(config.jaegerA, traceB.traceID),
    prometheusValue(config.prometheusA, 'count(tcc_conventional_requests_total)'),
    prometheusValue(config.prometheusB, 'count(tcc_conventional_requests_total)'),
    prometheusValue(config.prometheusA, 'count({__name__=~"online_boutique_otel_.*"})'),
    prometheusValue(config.prometheusB, 'count({__name__=~"online_boutique_otel_.*"})'),
    operationSpanCount(config.jaegerB, 'frontend', 'opentelemetry.proto.collector.trace.v1.TraceService/Export'),
    operationSpanCount(config.jaegerB, 'checkoutservice', 'opentelemetry.proto.collector.trace.v1.TraceService/Export'),
    operationSpanCount(config.jaegerB, 'productcatalogservice', 'opentelemetry.proto.collector.trace.v1.TraceService/Export')
  ]);

const bIdInLogsB = await waitForLokiMatches(config.lokiB, traceB.traceID);
const [bIdInLogsA, aIdInLogsA, aIdInLogsB, spanFieldsInLogsA, conventionalHeaderFieldsInLogsA] = await Promise.all([
  lokiMatches(config.lokiA, traceB.traceID),
  lokiMatches(config.lokiA, traceA.traceID),
  lokiMatches(config.lokiB, traceA.traceID),
  lokiQueryMatches(config.lokiA, '{service_name=~".+"} | span_id != ""'),
  lokiQueryMatches(config.lokiA, '{service_name=~".+"} | x_tcc_trace_id != ""')
]);

const metricValidation = {};
const durationValidation = {};
for (const service of applicationServices) {
  const metric = directMetricPatterns[service];
  const value = await waitForPrometheusValue(
    config.prometheusB, `${metric}{service_name="${service}"}`);
  metricValidation[service] = { metric, value };
  const durationMetric = durationMetricPatterns[service];
  const durationValue = await waitForPrometheusValue(
    config.prometheusB, `${durationMetric}{service_name="${service}"}`);
  durationValidation[service] = { metric: durationMetric, value: durationValue };
}

const logValidation = {};
for (const service of applicationServices) {
  logValidation[service] = await waitForServiceLog(service);
}

const [traceLabelA, spanLabelA, exemplarsA, traceLabelB, spanLabelB, spanMetricsB] = await Promise.all([
  prometheusValue(config.prometheusA, 'count({__name__=~"tcc_conventional_.*",trace_id=~".+"})'),
  prometheusValue(config.prometheusA, 'count({__name__=~"tcc_conventional_.*",span_id=~".+"})'),
  prometheusExemplars(config.prometheusA, 'tcc_conventional_request_duration_seconds_sum'),
  prometheusValue(config.prometheusB, 'count({__name__=~"online_boutique_otel_.*",trace_id=~".+"})'),
  prometheusValue(config.prometheusB, 'count({__name__=~"online_boutique_otel_.*",span_id=~".+"})'),
  prometheusValue(config.prometheusB, 'count({__name__=~".*span_metrics.*"})')
]);

assert(traceA.traceID !== traceB.traceID, 'A and B produced the same trace ID');
assert(Math.abs(startA - startB) < 100_000, 'A and B traces are not from the same request window');
assert(missingA.length === 0, `services missing from A: ${missingA.join(', ')}`);
assert(missingB.length === 0, `services missing from B: ${missingB.join(', ')}`);
assert(cartOperationsA.some(name => name.toLowerCase().includes('redis')), 'Redis span missing from A');
assert(cartOperationsB.some(name => name.toLowerCase().includes('redis')), 'Redis span missing from B');
assert(aInB === 0, 'trace ID A was found in Jaeger B');
assert(bInA === 0, 'trace ID B was found in Jaeger A');
assert(conventionalA > 0, 'conventional metrics missing from Prometheus A');
assert(conventionalB === 0, 'conventional metrics found in Prometheus B');
assert(otelA === 0, 'OTel metrics found in Prometheus A');
assert(otelB > 0, 'OTel metrics missing from Prometheus B');
assert(bIdInLogsA === 0, 'trace ID B found in Loki A');
assert(aIdInLogsA === 0, 'trace ID A used as structured correlation in Loki A');
assert(aIdInLogsB === 0, 'trace ID A found in Loki B');
assert(spanFieldsInLogsA === 0, 'structured span_id fields found in Loki A');
assert(conventionalHeaderFieldsInLogsA === 0, 'conventional trace headers found as structured fields in Loki A');
assert(bIdInLogsB > 0, 'trace-correlated OTel logs missing from Loki B');
for (const [service, result] of Object.entries(metricValidation)) {
  assert(result.value > 0, `direct OTel SDK request metric missing for ${service}`);
}
for (const [service, result] of Object.entries(durationValidation)) {
  assert(result.value > 0, `direct OTel SDK duration metric missing for ${service}`);
}
for (const [service, result] of Object.entries(logValidation)) {
  assert(result.matches > 0, `trace-correlated OTel log missing for ${service}`);
}
assert(traceLabelA === 0, 'Prometheus A contains a trace_id label');
assert(spanLabelA === 0, 'Prometheus A contains a span_id label');
assert(exemplarsA === 0, 'Prometheus A contains trace-linked exemplars');
assert(traceLabelB === 0, 'Prometheus B contains a trace_id metric label');
assert(spanLabelB === 0, 'Prometheus B contains a span_id metric label');
assert(spanMetricsB === 0, 'Prometheus B contains span-derived metric series');
assert(frontendExporterSpans === 0, 'frontend traced its own OTLP exporter');
assert(checkoutExporterSpans === 0, 'checkoutservice traced its own OTLP exporter');
assert(catalogExporterSpans === 0, 'productcatalogservice traced its own OTLP exporter');

console.log(JSON.stringify({
  status: 'PASS',
  sameRequestStartDeltaMicros: Math.abs(startA - startB),
  traceA: { id: traceA.traceID, spans: traceA.spans.length, services: servicesA, cartOperations: cartOperationsA },
  traceB: { id: traceB.traceID, spans: traceB.spans.length, services: servicesB, cartOperations: cartOperationsB },
  isolation: {
    traceAInJaegerB: aInB,
    traceBInJaegerA: bInA,
    traceBInLokiA: bIdInLogsA,
    traceAInLokiA: aIdInLogsA,
    traceAInLokiB: aIdInLogsB,
    traceBInLokiB: bIdInLogsB,
    structuredSpanFieldsInLokiA: spanFieldsInLogsA,
    structuredConventionalTraceHeadersInLokiA: conventionalHeaderFieldsInLogsA,
    conventionalSeriesInPrometheusA: conventionalA,
    conventionalSeriesInPrometheusB: conventionalB,
    otelSeriesInPrometheusA: otelA,
    otelSeriesInPrometheusB: otelB,
    prometheusATraceIdLabels: traceLabelA,
    prometheusASpanIdLabels: spanLabelA,
    prometheusAExemplars: exemplarsA,
    prometheusBTraceIdLabels: traceLabelB,
    prometheusBSpanIdLabels: spanLabelB,
    spanDerivedMetricsInPrometheusB: spanMetricsB,
    otlpExporterSpansInJaegerB: frontendExporterSpans + checkoutExporterSpans + catalogExporterSpans
  },
  directSdkMetrics: metricValidation,
  directSdkDurationMetrics: durationValidation,
  correlatedOtelLogs: logValidation
}, null, 2));
