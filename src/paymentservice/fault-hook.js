'use strict';

const fs = require('fs');

const DEFAULT_FLAG_PATH = '/tmp/tcc-faults/payment-charge-exception';

function throwIfChargeFailureEnabled(env = process.env, exists = fs.existsSync) {
  if (env.TCC_FAULT_INJECTION_ENABLED !== 'true') return;
  const flagPath = env.TCC_FAULT_FLAG_PATH || DEFAULT_FLAG_PATH;
  if (!exists(flagPath)) return;

  const error = new Error('Payment charge failed');
  error.code = 13; // gRPC INTERNAL
  throw error;
}

module.exports = { DEFAULT_FLAG_PATH, throwIfChargeFailureEnabled };
