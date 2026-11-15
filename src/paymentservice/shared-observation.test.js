'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { dispatchApplicationEvent, dispatchObservation } = require('./shared-observation');

test('one operation completion is delivered unchanged to both telemetry adapters', () => {
  const seenA = [];
  const seenB = [];
  const observation = {
    service: 'paymentservice',
    operation: 'PaymentService/Charge',
    kind: 'SERVER',
    statusCode: '13',
    outcome: 'ERROR',
    durationSeconds: 0.125
  };
  const delivered = dispatchObservation(observation, [
    { recordObservation: value => seenA.push(value) },
    { recordObservation: value => seenB.push(value) }
  ]);

  assert.equal(seenA.length, 1);
  assert.equal(seenB.length, 1);
  assert.strictEqual(seenA[0], seenB[0]);
  assert.strictEqual(delivered, seenA[0]);
  assert.equal(seenA[0].durationSeconds, 0.125);
  assert.equal(seenA[0].outcome, 'ERROR');
  assert.equal(Object.isFrozen(seenA[0]), true);
});

test('one application error is delivered to both adapters without treatment clues', () => {
  const seenA = [];
  const seenB = [];
  dispatchApplicationEvent({
    eventName: 'payment_charge_failed',
    severity: 'ERROR',
    operation: 'PaymentService/Charge',
    errorType: 'Error',
    errorMessage: 'Payment charge failed',
    message: 'Payment charge failed'
  }, [
    { emitApplicationEvent: value => seenA.push(value) },
    { emitApplicationEvent: value => seenB.push(value) }
  ]);

  assert.deepEqual(seenA, seenB);
  assert.equal(seenA.length, 1);
  assert.doesNotMatch(JSON.stringify(seenA[0]), /controlled|injected|experiment|chaos|ground.?truth|F[1-5]/i);
});
