# Shared Observation Contract — Static Validation

No physical fault was run. Existing F4/F5 RAW was reprocessed read-only and its hashes were checked against each Dataset manifest.

## Gates

- Python experimental suite: 91 passed.
- Cart service .NET tests: not run; `dotnet` is not installed in this environment.
- Service coverage: PARTIAL; multiple services do not yet show paired adapters from one measurement point.
- Existing F4/F5 reprocess: FAIL; see `existing-f4-f5-reprocess.md/json`.
- Fresh F4/F5 and final F1–F5 battery: NOT STARTED because the reprocess gate failed.

## F4 — `iv-edv-so-115b-001-f4-f4`

- Existing RAW manifest hashes valid: PASS
- Shared marked source events in A/B: 0/0
- Dataset-to-request provenance A/B: True/True
- Metric parity A-only/B-only: 44/0
- Log parity: PASS (both requests have zero logs because old RAW lacks the explicit event marker)
- Trace parity: PASS

## F5 — `iv-edv-so-115b-002-f5-f5`

- Existing RAW manifest hashes valid: PASS
- Shared marked source events in A/B: 0/0
- Dataset-to-request provenance A/B: True/True
- Metric parity A-only/B-only: 48/0
- Log parity: PASS (both requests have zero logs because old RAW lacks the explicit event marker)
- Trace parity: PASS

## Decision

STOP before physical validation. Existing RAW has no explicitly marked Shared Observation Events, and the current approved-metric projection leaves A-only metric concepts. These are source coverage/data vintage mismatches; no message-specific filtering or RAW edits were applied.
