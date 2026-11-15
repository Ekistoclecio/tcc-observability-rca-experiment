'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chargeExceptionActive, throwControlledChargeException } = require('./fault-hook');

test('charge hook is inactive unless the experimental flag is enabled and file exists', () => {
  assert.equal(chargeExceptionActive({ TCC_FAULT_INJECTION_ENABLED: 'false' }, () => true), false);
  assert.equal(chargeExceptionActive({ TCC_FAULT_INJECTION_ENABLED: 'true' }, () => false), false);
  assert.equal(chargeExceptionActive({ TCC_FAULT_INJECTION_ENABLED: 'true' }, () => true), true);
});

test('enabled Charge hook returns a deterministic gRPC INTERNAL error', () => {
  assert.throws(
    () => throwControlledChargeException({ TCC_FAULT_INJECTION_ENABLED: 'true' }, () => true),
    error => error.code === 13 && error.message === 'Payment charge failed' && !/controlled|injected|experiment|chaos|F[1-5]/i.test(error.message),
  );
});

test('the default hook remains inactive when the experiment environment is unset', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tcc-fault-hook-'));
  const flagPath = path.join(directory, 'payment-charge-exception');
  try {
    fs.writeFileSync(flagPath, 'enabled');
    assert.equal(chargeExceptionActive({ TCC_FAULT_FLAG_PATH: flagPath }, fs.existsSync), false);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
