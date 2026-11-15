# Shared Observation Contract

Both telemetry scenarios must originate from the same semantic application observations. Each application observation is measured once, then passed to the conventional and OpenTelemetry adapters. The adapters may encode the observation differently and keep independent identifiers and propagation context.

```text
application boundary
       │
       ├── one duration and outcome
       ├── conventional adapter: Prometheus, stdout, custom trace context
       └── OTel adapter: OTel metrics/logs/traces, W3C context
```

## Observation points

`REQUEST` records request start and completion. Completion contains service, operation, duration, and outcome.

`DOWNSTREAM_CALL` records a client operation with source service, target service, operation, duration, and outcome.

`DATASTORE_OPERATION` records a datastore operation with service, datastore, operation, duration, and outcome.

`APPLICATION_EVENT` records application errors and warnings plus failed downstream and datastore operations. An error at a covered operation is emitted by the same completion observation to whichever adapter is active. Application messages do not mention the fault harness or fault class.

The machine-readable contract is [observation-contract.json](/home/ekistoclecio/dev/microservices-demo/config/experiment/observation-contract.json).

## Metric and span adapters

The semantic measurement is computed once. `request_count` and `request_duration` use service, operation, kind, outcome, and target where applicable. F1 CPU uses the same cAdvisor container CPU measurements in both collection pipelines.

The same logical boundaries produce `SERVER`, `CLIENT`, and `DATASTORE` spans. A retains custom trace/span identifiers and `x-tcc-trace-id`/`x-tcc-span-id` propagation. B retains OTel IDs, W3C propagation, semantic conventions, and native log-to-span correlation. IDs are never shared across scenarios.

## Automatic instrumentation

Each automatic instrument is classified as `REQUIRED_BY_SHARED_CONTRACT`, `CORRELATION_ONLY`, `RAW_AUDIT_ONLY`, or `UNNECESSARY`. A second automatic span or metric for an already represented operation is disabled or excluded from the Common RCA View. Runtime, SDK, exporter, and framework output remains available in RAW when it is naturally produced.

## Fault independence

Observation code does not inspect Ground Truth, the fault class, location, target, or IDs such as F1–F5. It responds only to real operation completion, status, duration, and application errors. Observation code must not change application responses, retry behavior, or fault behavior.

## Validation

Before running faults, adapter tests verify that one semantic completion is offered to both adapters with the same duration and outcome, while each adapter keeps independent propagation. A shared-observation parity validator compares operation keys and application event classes without comparing trace IDs. Only after static tests pass should F4/F5 be used to check exception and datastore paths physically.

## Fault coverage map

| Fault | Required evidence from shared observations |
|---|---|
| F1 CPU saturation | `cpu_utilization`, request duration, request count/outcome |
| F2 network latency | checkout-to-payment client operation duration/outcome |
| F3 service unavailable | checkout-to-payment client operation error |
| F4 application exception | payment `Charge` server error and `payment_charge_failed` application event |
| F5 datastore unavailable | cart-to-Redis datastore operation error |

This contract defines application facts; the F1–F5 mapping is only a validation checklist and is never supplied to application instrumentation.

## Runtime adoption status

The contract is implemented incrementally and must not be treated as fully adopted until every runtime listed in [auto-instrumentation-inventory.json](/home/ekistoclecio/dev/microservices-demo/config/experiment/auto-instrumentation-inventory.json) reports shared completion observations. The current code shares the RPC measurement adapter in Node.js `paymentservice`/`currencyservice`, Go `checkoutservice`, and .NET `cartservice`; it also shares Redis datastore measurement completion in CartService. Payment application failures, checkout downstream failures, and Cart Redis failures are emitted from their operation paths to the scenario-specific adapters.

Go `frontend`, `productcatalogservice`, and `shippingservice`, Python services, and Java `adservice` currently retain separate conventional and OpenTelemetry framework handlers. They observe the same RPC lifecycle, but do not yet pass one immutable semantic completion object to both metric adapters. This is an explicit implementation gap. Do not claim system-wide end-to-end “by construction” parity until this gap is closed and the relevant language suites pass.

## Implemented operation adapters

For Node.js RPC handlers, the conventional wrapper owns the start/end boundary and constructs one completion observation with service, method, kind, gRPC status, semantic outcome, and duration. It records the conventional metric when enabled and passes the same frozen observation to the OTel metric adapter. OTel gRPC instrumentation remains responsible for native B trace context and spans; the custom wrapper does not create A identifiers in B.

For CartService RPCs, one interceptor computes duration and outcome, records the OTel metric, and passes the same duration/outcome into the conventional span adapter when enabled. For RedisCartStore, one elapsed duration and failure outcome feed the OTel meter and conventional client span; an application error log is emitted from that same failed operation path.

For checkoutservice client/server RPCs, the conventional interceptor measures the completion once even when the conventional exporter is disabled. It sends the same duration/status to the OTel semantic metric adapter and the conventional metric/span adapter when enabled. A failed client call emits one structured `downstream_call_failed` event from the shared completion point, using the current RPC context so B retains native correlation while A remains independent.

PaymentService `Charge` emits `payment_charge_failed` at the real application exception boundary. Both adapters receive the same event object; A writes a conventional structured log without trace/span identifiers, while B emits an OTel log that may inherit the active native OTel context. The injected exception message is neutral and does not mention the test harness.
