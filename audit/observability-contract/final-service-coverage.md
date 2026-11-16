# Cobertura estática do Shared Observation Contract

Resultado: **PASS**. Execução de faults permitida: **True**.

| Serviço | Estado | SERVER | CLIENT | DATASTORE | APPLICATION_EVENT | request_count | request_duration | Adaptadores A/B |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `frontend` | **COMPLETE_WITH_NA** | sim | sim | N/A | N/A | conventional, opentelemetry |
| `checkoutservice` | **COMPLETE_WITH_NA** | sim | sim | N/A | sim | conventional, opentelemetry |
| `cartservice` | **COMPLETE_WITH_NA** | sim | N/A | sim | sim | conventional, opentelemetry |
| `productcatalogservice` | **COMPLETE_WITH_NA** | sim | N/A | N/A | N/A | conventional, opentelemetry |
| `currencyservice` | **COMPLETE_WITH_NA** | sim | N/A | N/A | N/A | conventional, opentelemetry |
| `paymentservice` | **COMPLETE_WITH_NA** | sim | N/A | N/A | sim | conventional, opentelemetry |
| `shippingservice` | **COMPLETE_WITH_NA** | sim | N/A | N/A | N/A | conventional, opentelemetry |
| `emailservice` | **COMPLETE_WITH_NA** | sim | N/A | N/A | N/A | conventional, opentelemetry |
| `recommendationservice` | **COMPLETE_WITH_NA** | sim | sim | N/A | N/A | conventional, opentelemetry |
| `adservice` | **COMPLETE_WITH_NA** | sim | N/A | N/A | N/A | conventional, opentelemetry |

## Evidência e N/A

### frontend

Fontes: `src/frontend/conventional.go`, `src/frontend/otel_telemetry.go`, `src/frontend/main.go`.
N/A: `datastore` — No direct datastore access; datastore work is observed in cartservice.; `application_events` — No service-specific error event is required by the current fault contract; request outcomes remain shared..
Source markers: todos encontrados.

### checkoutservice

Fontes: `src/checkoutservice/conventional.go`, `src/checkoutservice/otel_telemetry.go`.
N/A: `datastore` — No direct datastore access; Redis is observed in cartservice..
Source markers: todos encontrados.

### cartservice

Fontes: `src/cartservice/src/ConventionalTelemetry.cs`, `src/cartservice/src/Telemetry.cs`, `src/cartservice/src/cartstore/RedisCartStore.cs`.
N/A: `client_downstream` — No application RPC client; Redis access is declared under DATASTORE..
Source markers: todos encontrados.

### productcatalogservice

Fontes: `src/productcatalogservice/conventional.go`, `src/productcatalogservice/otel_telemetry.go`, `src/productcatalogservice/server.go`.
N/A: `client_downstream` — No application RPC client.; `datastore` — No direct datastore access.; `application_events` — No service-specific application event is required by the fault contract..
Source markers: todos encontrados.

### currencyservice

Fontes: `src/currencyservice/conventional-telemetry.js`, `src/currencyservice/otel-telemetry.js`, `src/currencyservice/server.js`.
N/A: `client_downstream` — No application RPC client.; `datastore` — No direct datastore access.; `application_events` — No service-specific application event is required by the fault contract..
Source markers: todos encontrados.

### paymentservice

Fontes: `src/paymentservice/conventional-telemetry.js`, `src/paymentservice/otel-telemetry.js`, `src/paymentservice/server.js`.
N/A: `client_downstream` — No application RPC client.; `datastore` — No direct datastore access..
Source markers: todos encontrados.

### shippingservice

Fontes: `src/shippingservice/conventional.go`, `src/shippingservice/otel_telemetry.go`, `src/shippingservice/main.go`.
N/A: `client_downstream` — No application RPC client.; `datastore` — No direct datastore access.; `application_events` — No service-specific application event is required by the fault contract..
Source markers: todos encontrados.

### emailservice

Fontes: `src/emailservice/conventional_telemetry.py`, `src/emailservice/otel_telemetry.py`, `src/emailservice/email_server.py`.
N/A: `client_downstream` — No application RPC client.; `datastore` — No direct datastore access.; `application_events` — No service-specific application event is required by the fault contract..
Source markers: todos encontrados.

### recommendationservice

Fontes: `src/recommendationservice/conventional_telemetry.py`, `src/recommendationservice/otel_telemetry.py`, `src/recommendationservice/recommendation_server.py`.
N/A: `datastore` — No direct datastore access.; `application_events` — No service-specific application event is required by the fault contract..
Source markers: todos encontrados.

### adservice

Fontes: `src/adservice/src/main/java/hipstershop/ConventionalTelemetry.java`, `src/adservice/build.gradle`, `src/adservice/src/main/java/hipstershop/AdService.java`.
N/A: `client_downstream` — No application RPC client.; `datastore` — No direct datastore access.; `application_events` — No service-specific application event is required by the fault contract..
Source markers: todos encontrados.


RAWs históricos não foram usados para validar source parity: `NOT_APPLICABLE_OLD_INSTRUMENTATION`.
