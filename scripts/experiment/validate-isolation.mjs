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

async function lokiMatches(baseUrl, traceId) {
  const url = new URL('/loki/api/v1/query_range', baseUrl);
  url.searchParams.set('query', `{service_name=~".+"} |= "${traceId}"`);
  url.searchParams.set('start', String((config.startMicros - 5_000_000) * 1000));
  url.searchParams.set('end', String((config.endMicros + 10_000_000) * 1000));
  url.searchParams.set('limit', '100');
  const response = await json(url);
  return (response.data?.result || []).reduce((total, stream) => total + stream.values.length, 0);
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

const [aInB, bInA, conventionalA, conventionalB, otelA, otelB, bIdInLogsA, aIdInLogsB,
  frontendExporterSpans, checkoutExporterSpans, catalogExporterSpans] =
  await Promise.all([
    traceCount(config.jaegerB, traceA.traceID),
    traceCount(config.jaegerA, traceB.traceID),
    prometheusValue(config.prometheusA, 'count(tcc_conventional_requests_total)'),
    prometheusValue(config.prometheusB, 'count(tcc_conventional_requests_total)'),
    prometheusValue(config.prometheusA, 'count({__name__=~"online_boutique_otel_.*"})'),
    prometheusValue(config.prometheusB, 'count({__name__=~"online_boutique_otel_.*"})'),
    lokiMatches(config.lokiA, traceB.traceID),
    lokiMatches(config.lokiB, traceA.traceID),
    operationSpanCount(config.jaegerB, 'frontend', 'opentelemetry.proto.collector.trace.v1.TraceService/Export'),
    operationSpanCount(config.jaegerB, 'checkoutservice', 'opentelemetry.proto.collector.trace.v1.TraceService/Export'),
    operationSpanCount(config.jaegerB, 'productcatalogservice', 'opentelemetry.proto.collector.trace.v1.TraceService/Export')
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
assert(aIdInLogsB === 0, 'trace ID A found in Loki B');
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
    traceAInLokiB: aIdInLogsB,
    conventionalSeriesInPrometheusA: conventionalA,
    conventionalSeriesInPrometheusB: conventionalB,
    otelSeriesInPrometheusA: otelA,
    otelSeriesInPrometheusB: otelB,
    otlpExporterSpansInJaegerB: frontendExporterSpans + checkoutExporterSpans + catalogExporterSpans
  }
}, null, 2));
