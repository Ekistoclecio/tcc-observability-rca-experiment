'use strict';

const fs = require('fs');

const DEFAULT_FLAG_PATH = '/tmp/tcc-faults/payment-charge-exception';

function chargeExceptionActive(env = process.env, exists = fs.existsSync) {
  if (env.TCC_FAULT_INJECTION_ENABLED !== 'true') return false;
  return exists(env.TCC_FAULT_FLAG_PATH || DEFAULT_FLAG_PATH);
}

function throwControlledChargeException(env = process.env, exists = fs.existsSync) {
  if (!chargeExceptionActive(env, exists)) return;
  const error = new Error('controlled application fault in PaymentService.Charge');
  error.code = 13; // gRPC INTERNAL
  throw error;
}

module.exports = { DEFAULT_FLAG_PATH, chargeExceptionActive, throwControlledChargeException };
