# Shared Observation Coverage — Static Audit

This audit records the implementation state declared in `config/experiment/observation-contract.json`. `implemented` means both A and B adapters currently share the relevant source point; `partial` records declared application coverage that still lacks a demonstrated paired shared adapter or signal.

| Service | SERVER | CLIENT | DATASTORE | Shared event | Metric adapters | Status |
|---|---:|---:|---:|---:|---|---|
| frontend | yes | yes | n/a | no | conventional only | partial |
| checkoutservice | yes | yes | n/a | no (legacy A event is unmarked) | conventional + OTel | partial |
| cartservice | yes | n/a | yes | yes | conventional + OTel | partial |
| productcatalogservice | yes | n/a | n/a | no | conventional only | partial |
| currencyservice | yes | n/a | n/a | no | conventional + OTel | implemented |
| paymentservice | yes | n/a | n/a | yes | conventional + OTel | implemented |
| shippingservice | yes | n/a | n/a | no | conventional only | partial |
| emailservice | yes | n/a | n/a | no | conventional only | partial |
| recommendationservice | yes | yes | n/a | no | conventional only | partial |
| adservice | yes | n/a | n/a | no | conventional only | partial |

## Assessment

Coverage is explicitly inventoried, but is not complete. In particular, the services marked `partial` do not yet prove paired metric adapters from the same completion object. Therefore the Shared Observation Contract is **not ready for physical smoke validation**. The matrix is not evidence that those missing adapters are implemented.
