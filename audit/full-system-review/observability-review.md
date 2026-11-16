# Revisão de observabilidade

## A e B reais

A usa Prometheus/client convencional, stdout tradicional e tracing custom com x-tcc-trace-id / x-tcc-span-id. B usa OTel SDK/agentes, OTLP, W3C e correlação. Um único SUT serve os dois conjuntos de backends separados. A distinção de tratamento ainda está presente. O Shared Observation Contract é semântico, com implementação idiomática por linguagem, não uma biblioteca binária única.

| Serviço | Linguagem | A | B | Risco/ressalva |
|---|---|---|---|---|
| frontend | Go | middleware, métricas, tracing custom | OTel HTTP/gRPC | execução do código atual não provada |
| checkoutservice | Go | interceptors server/client | observadores OTel/gRPC | unidade request/attempt precisa ser explícita |
| cartservice | .NET | Prometheus/Zipkin custom | ASP.NET OTel, ActivitySource Redis, Meter/log | host sem dotnet; build atual não comprovado |
| productcatalogservice | Go | server metrics/spans | OTel server | cobertura só estática |
| currencyservice | Node | wrapper custom | callbacks e auto gRPC | spans automáticos podem duplicar boundary |
| paymentservice | Node | wrapper e evento de erro | callbacks/auto instrumentation | marker ausente em F4 |
| shippingservice | Go | middleware server | observação OTel | N/A de CLIENT/DATASTORE precisa corresponder à topologia |
| emailservice | Python | interceptor custom | OTel observer | sem build/runtime atual comprovado |
| recommendationservice | Python | middleware/client custom | OTel | mesma lacuna de execução |
| adservice | Java | interceptor convencional | Java OTel agent | verificar spans manuais + automáticos |

Classificação do checker como COMPLETE_WITH_NA não comprova build, deploy, boundaries equivalentes ou sobrevivência no backend. Outro relatório deixa faults BLOCKED/PARTIAL: há duas fontes contraditórias.

## Evento payment_charge_failed

Caminho esperado: Charge → evento → adapters A/B → logs → RAW → marker gate → Dataset → request. Na Fresh F4, RAW tem eventos sem marker; o Builder excluiu corretamente segundo o gate, mas o runner permitiu Dataset vazio e overall PASS. Nenhuma regra nos pipelines verificados explica a remoção. Imagem anterior ao marker é hipótese provável, não prova.

## Métricas

Scrape Prometheus A/B: 5 s; reader OTel: 5 s. Builder query_range step=1s é grade, não resolução. Na F4, baseline 42 em ambos; payment success no cutoff A=52 (+10), B=49 (+7); checkout success A=53 (+11), B=48 (+6). Cerca de 16 s depois B converge para A. Isso prova atraso compatível com export/scrape, não prova completude da janela nem semântica de retry. Traces não devem corrigir counters. Definir request lógica versus tentativa/retry.

## Logs e treatment

Shared Events only pode ser metodologicamente válido se declarar a fonte de logs dos dois braços e as evidências F1–F5 existirem nos sinais do contrato. Mas marker ausente deve falhar, não virar Dataset com zero logs. O marker pode ser metadado interno, mas exige teste de serialização/pipeline. Logs legados devem ficar em RAW.

## Contrato mínimo de evidência

| Fault | Evidência requerida |
|---|---|
| F1 | CPU cAdvisor comum em checkout; duration e count/outcome |
| F2 | CLIENT checkout→payment, duração comparável |
| F3 | mesma edge, ERROR/unavailable |
| F4 | payment Charge SERVER ERROR e payment_charge_failed A/B |
| F5 | cartservice→redis-cart GET DATASTORE ERROR e datastore_operation_failed |

RAW mais amplo é aceitável; fatos model-visible exclusivos não. O atual não demonstra F4.

