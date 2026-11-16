# Shared Observation Contract

## Objetivo

O contrato define pontos semânticos comuns para os dez serviços de aplicação do Online Boutique. Cada completion mede uma operação uma vez e encaminha a mesma duração e o mesmo outcome aos adapters habilitados. O cenário A continua convencional e mantém propagação própria; o cenário B continua usando OpenTelemetry e contexto W3C.

```text
operation completion
        ├── adapter convencional (A)
        └── adapter OpenTelemetry (B)
```

A variável experimental continua sendo estrutura, padronização, propagação de contexto e correlação entre sinais. O Builder normaliza e compacta os dados; ele não escolhe fatos para fazer A e B parecerem equivalentes.

## Observation points

| Conceito | Campos do completion | Aplicação |
|---|---|---|
| `REQUEST` | service, operation, duration, outcome | Operação recebida por um serviço |
| `DOWNSTREAM_CALL` | source, target, operation, duration, outcome | Chamada RPC cliente |
| `DATASTORE_OPERATION` | service, datastore, operation, duration, outcome | Operação de `cartservice` em `redis-cart` |
| `APPLICATION_EVENT` | event_name, service, severity, operation, outcome e erro opcional | Erro de cobrança e erro de datastore compartilhados |

Os serviços sem uma modalidade recebem `N/A` e justificativa na matriz [shared-observation-coverage.json](../../config/experiment/observation-contract.json). `loadgenerator` não faz parte da telemetria RCA; `redis-cart` é observado pelo adapter de `cartservice`.

## Adapters e tratamento

O adapter A usa métricas convencionais, stdout/logging tradicional, spans customizados e `x-tcc-trace-id`/`x-tcc-span-id`. O adapter B usa instrumentos e APIs OTel, instrumentação gRPC/HTTP e W3C Trace Context. IDs são independentes. B pode carregar `trace_id`/`span_id` em logs; A não recebe correlação sintética.

Eventos de aplicação têm `event_name` estável e marcador técnico interno. O marcador não é model-visible. Mensagem renderizada não é identidade factual. Logs legados, runtime, framework e SDK permanecem no RAW e não viram eventos RCA implicitamente.

## Métricas e sinais automáticos

Os conceitos model-visible aprovados são `request_count`, `request_duration` e `cpu_utilization`. As medições RPC/HTTP e datastore vêm dos boundaries declarados; métricas OTel automáticas duplicadas permanecem audit-only. A utilização de CPU usa a mesma fonte cAdvisor nas consultas Prometheus A e B.

Counters/histogram counts com baseline válido e delta zero são omitidos simetricamente. `missing_baseline` permanece desconhecido e nunca vira zero. Métricas RAW, famílias nativas, SDK metadata e spans automáticos não são apagados.

## Validação

A auditoria estática lista paths de código e `N/A` por serviço. Ela não substitui a source-parity validation de execuções novas. RAW F4/F5 anterior foi produzido antes dos markers atuais e não é gate de source parity. A próxima ação permitida é uma smoke física F4/F5 somente quando a suíte aprovada e a auditoria estática estiverem completas; a bateria F1–F5 depende do resultado dessa smoke. Nenhuma fault foi executada por este documento.
