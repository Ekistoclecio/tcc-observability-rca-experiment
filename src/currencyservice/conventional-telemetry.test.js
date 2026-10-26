'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { extractContext } = require('./conventional-telemetry');

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
