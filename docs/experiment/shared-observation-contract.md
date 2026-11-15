# Shared Observation Contract

The experiment intends to hold diagnostic facts constant while varying telemetry representation, context propagation, and cross-signal correlation. Both variants should receive one logical observation from the application boundary; conventional and OpenTelemetry adapters then encode it independently.

## Model-visible source policy

The Common RCA View includes logs only when they are explicitly marked as a Shared Application Event and have an allowlisted `event_name` plus diagnostic severity. Arbitrary stdout, application informational logs, framework/runtime logs, and unmarked errors stay in RAW and are `RAW_AUDIT_ONLY`. Event identity is `service`, canonical severity, `event_name`, `operation`, `outcome`, and `error_type`; rendered message and correlation IDs are not identity fields. The internal `observation_event` marker is removed before model-visible serialization.

Metrics are restricted to approved shared semantic concepts. Automatic OTel runtime/library metrics remain RAW-only. A cumulative counter or histogram count with a valid baseline and zero delta is omitted; a missing baseline remains unknown and is never changed to zero. Gauges are not subject to zero-delta omission.

Logical traces preserve SERVER, CLIENT, and DATASTORE boundaries. B may retain OTel/W3C correlation fields; A does not receive synthetic OTel identifiers.

## Current static coverage

See [shared observation coverage audit](../../audit/observability-contract/shared-observation-coverage.md) and the machine-readable `service_coverage` in `config/experiment/observation-contract.json`. Several services remain partial, so no new physical validation should run until paired source observations are implemented and tested for all relevant services.
