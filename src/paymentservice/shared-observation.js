'use strict';

// Dispatch one immutable semantic observation to independent telemetry adapters.
// Adapters own their encodings and correlation IDs; this function owns no IDs.
function dispatchObservation(observation, adapters) {
  const value = Object.freeze({ ...observation });
  for (const adapter of adapters) adapter?.recordObservation?.(value);
  return value;
}

function dispatchApplicationEvent(event, adapters) {
  const value = Object.freeze({ ...event });
  for (const adapter of adapters) adapter?.emitApplicationEvent?.(value);
  return value;
}

module.exports = { dispatchObservation, dispatchApplicationEvent };
