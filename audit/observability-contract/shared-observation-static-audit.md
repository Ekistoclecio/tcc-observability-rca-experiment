# Shared Observation Contract — Static Implementation Audit

## Scope

This audit records source-level wiring before any new physical fault run. It distinguishes implemented shared measurement points from existing independent framework instrumentation.

## Implemented in this change

| Runtime/service | Observation | A adapter | B adapter | Shared measurement |
|---|---|---|---|---|
| Node.js `paymentservice` | gRPC `Charge` completion | Conventional counter + Zipkin server span | OTel counter/histogram; gRPC instrumentation span | One outer wrapper calculates duration/status and supplies one observation |
| Node.js `currencyservice` | gRPC server completion | Conventional counter + Zipkin server span | OTel counter/histogram; gRPC instrumentation span | One outer wrapper calculates duration/status and supplies one observation |
| Go `checkoutservice` | gRPC server and downstream completion | Conventional counter + Zipkin spans | Shared OTel request counters/histograms; gRPC instrumentation spans | One interceptor completion calculates status/duration; client errors emit one shared application event |
| .NET `cartservice` | gRPC server completion | Conventional counter + Zipkin server span | CartTelemetry OTel counter/histogram | One interceptor calculates duration/status and records both adapters |
| .NET `cartservice` | Redis operations | Conventional client span | ActivitySource, OTel meter, OTel logger | One operation completion calculates duration/outcome for both metric/span adapters and emits one failure event |
| Node.js `paymentservice` | `Charge` application failure | Structured stdout event, no correlation IDs | OTel Logs API event, native active context may correlate | One immutable event is dispatched to both adapters |

The payment failure message is now `Payment charge failed`; gRPC status behavior remains INTERNAL. No fault class, Ground Truth, target, or experiment wording is emitted in the application event.

## Adoption gaps

Go `frontend`, `productcatalogservice`, and `shippingservice` still use conventional interceptors and OTel gRPC handlers independently. Python services use conventional interceptors and OTel gRPC instrumentation independently. Java `adservice` uses `ConventionalTelemetry` and the OTel Java agent independently. Those paths have not yet been converted to one shared semantic completion object for both metric adapters. The full system-wide adoption is therefore incomplete; the immediate F4/F5 paths are wired, but the cross-runtime test gate has not passed yet.

## Automatic instrumentation classification

The machine-readable inventory is `config/experiment/auto-instrumentation-inventory.json`. Runtime/library diagnostics remain RAW/audit-only under the current Common RCA View. OTel gRPC or ASP.NET server boundaries are required for native B traces. Node payment Pino log export is disabled because application events are explicitly emitted through the OTel Logs API. .NET runtime metrics are retained as RAW audit data and are not part of the Common RCA metric contract.

## Test and execution gate

Executed before fault runs:

- Experimental Python modules: 103 passed.
- Node paymentservice/currencyservice: 10 passed.
- Python emailservice/recommendationservice: 4 passed.
- Go checkoutservice: passed with `go test -vet=off ./...` under Go 1.27.1. Default `go test ./...` also runs vet and is blocked by the pre-existing non-constant format string warning at `src/checkoutservice/main.go:242`.
- .NET CartService: 3 passed under .NET SDK 10.0.

No fault was executed during this static audit. F4/F5 smoke is gated on rebuilding/loading the four changed application images and confirming rollout. The broader non-fault-path Go/Python/Java adoption gaps remain an explicit limitation; the smoke will exercise the shared observation paths for checkout downstream calls, PaymentService.Charge, and CartService/Redis.
