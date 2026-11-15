'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createConventionalTelemetry, extractContext } = require('./conventional-telemetry');

test('extracts only the independent conventional context', () => {
  const values = new Map([
    ['x-tcc-trace-id', ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']],
    ['x-tcc-span-id', ['bbbbbbbbbbbbbbbb']],
    ['traceparent', ['00-cccccccccccccccccccccccccccccccc-dddddddddddddddd-01']],
    ['tracestate', ['vendor=value']]
  ]);

  assert.deepEqual(extractContext({ get: key => values.get(key) || [] }), {
    traceId: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    parentSpanId: 'bbbbbbbbbbbbbbbb'
  });
});

test('does not derive conventional context from W3C propagation', () => {
  const values = new Map([
    ['traceparent', ['00-cccccccccccccccccccccccccccccccc-dddddddddddddddd-01']]
  ]);

  assert.deepEqual(extractContext({ get: key => values.get(key) || [] }), {
    traceId: undefined,
    parentSpanId: undefined
  });
});

test('shared server measurement sends one completion to the second adapter', async () => {
  const oldEnabled = process.env.ENABLE_CONVENTIONAL_TELEMETRY;
  delete process.env.ENABLE_CONVENTIONAL_TELEMETRY;
  try {
    const telemetry = createConventionalTelemetry('paymentservice');
    const observations = [];
    const handler = telemetry.wrapUnary('/hipstershop.PaymentService/Charge', (_call, callback) => {
      callback(Object.assign(new Error('Payment charge failed'), { code: 13 }));
    }, observation => observations.push(observation));
    const result = await new Promise(resolve => handler({ metadata: null }, error => resolve(error)));

    assert.equal(result.code, 13);
    assert.equal(observations.length, 1);
    assert.equal(observations[0].outcome, 'ERROR');
    assert.equal(observations[0].statusCode, '13');
    assert.equal(observations[0].service, 'paymentservice');
    assert.equal(observations[0].operation, '/hipstershop.PaymentService/Charge');
    assert.ok(observations[0].durationSeconds >= 0);
  } finally {
    if (oldEnabled === undefined) delete process.env.ENABLE_CONVENTIONAL_TELEMETRY;
    else process.env.ENABLE_CONVENTIONAL_TELEMETRY = oldEnabled;
  }
});
