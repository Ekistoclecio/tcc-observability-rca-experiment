'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { throwIfChargeFailureEnabled } = require('./fault-hook');

test('Charge failure hook is inactive unless explicitly enabled and flagged', () => {
  assert.doesNotThrow(() =>
    throwIfChargeFailureEnabled({ TCC_FAULT_INJECTION_ENABLED: 'false' }, () => true),
  );
  assert.doesNotThrow(() =>
    throwIfChargeFailureEnabled({ TCC_FAULT_INJECTION_ENABLED: 'true' }, () => false),
  );
});

test('Charge failure hook raises a neutral gRPC INTERNAL error when enabled', () => {
  assert.throws(
    () =>
      throwIfChargeFailureEnabled(
        { TCC_FAULT_INJECTION_ENABLED: 'true' },
        () => true,
      ),
    (error) => error.code === 13 && error.message === 'Payment charge failed',
  );
});

test('Charge failure hook uses the configured path when provided', () => {
  const checked = [];
  assert.throws(
    () =>
      throwIfChargeFailureEnabled(
        {
          TCC_FAULT_INJECTION_ENABLED: 'true',
          TCC_FAULT_FLAG_PATH: '/tmp/test-charge-failure',
        },
        (path) => {
          checked.push(path);
          return true;
        },
      ),
  );
  assert.deepEqual(checked, ['/tmp/test-charge-failure']);
});
