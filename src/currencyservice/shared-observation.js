'use strict';

// This service-local copy keeps the container build context self-contained.
function dispatchObservation(observation, adapters) {
  const value = Object.freeze({ ...observation });
  for (const adapter of adapters) adapter?.recordObservation?.(value);
  return value;
}

module.exports = { dispatchObservation };
