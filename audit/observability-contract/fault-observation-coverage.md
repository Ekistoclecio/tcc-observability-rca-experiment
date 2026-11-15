# Fault-to-Observation Coverage — Static Design Audit

This table records the minimum contract signals and current implementation evidence. It is not a claim of successful physical validation.

| Fault class | Required contract evidence | Current static state | Gate |
|---|---|---|---|
| `cpu_saturation` | `cpu_utilization`, request duration, request count/outcome | cAdvisor CPU source is shared; paired request observation coverage is incomplete | BLOCKED |
| `network_latency` | checkoutservice → paymentservice CLIENT duration/outcome | shared checkout client observation exists; all-service source parity remains incomplete | PARTIAL |
| `service_unavailable` | checkoutservice → paymentservice CLIENT ERROR | shared checkout client observation exists; event and paired source parity need run evidence | PARTIAL |
| `application_exception` | paymentservice/Charge SERVER ERROR and `payment_charge_failed` | source code offers event to A/B adapters; old artifacts contain no explicit marker | BLOCKED |
| `datastore_unavailable` | cartservice → redis-cart DATASTORE ERROR and `datastore_operation_failed` | shared cart event adapter now encodes a single semantic object; old artifacts lack marker and source parity is not demonstrated | BLOCKED |

The service matrix is intentionally explicit about partial adoption. F4/F5 raw reprocessing failed on missing shared event markers and model-visible metric parity. No new fault was executed after that gate failed.
