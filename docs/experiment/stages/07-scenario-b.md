# Stage 07 — Scenario B: OpenTelemetry

## Objetivo

Instrumentar os mesmos serviços e operações com OpenTelemetry, propagação W3C e sinais estruturados sem adicionar eventos artificiais.

## Implementação

`observability-b/` configura SDKs/collectors e exportação OTLP de métricas, logs e traces. Integração por linguagem segue APIs idiomáticas: bridge OTel-Logrus em Go; instrumentation Pino/OTel em Node; provider `ILogger` em .NET; `LoggingHandler` em Python; Java agent para Log4j2. Logs A continuam no caminho convencional sem mixin de IDs; logs B são `LogRecord`s OTLP.

## Correlação log↔trace

A entry Logrus inicial era criada antes de `otelmux` abrir o SERVER span, deixando handler logs sem IDs. A correção localizada atualiza a entry com o contexto atual da request dentro do handler. Um log natural durante span ativo passa pela integração OTel Logs com trace/span reais correspondentes ao span; fora de span, IDs continuam ausentes. O middleware não é reordenado e nenhum ID é copiado manualmente.

## Inicialização e decisões

Java agent é legível por UID não-root, usa `/tmp` com filesystem read-only e tem startup probe compatível com inicialização instrumentada. Campos Log4j literais `${ctx:traceId}` não são correlação e são excluídos. Evento de erro natural `Charge` atravessa o logger do serviço. Não há segunda chamada manual de logging por evento.

## Validação e invariantes

Testar SDK/exporter, labels e correlação comparando IDs do LogRecord com SERVER span real. Diferenças de contagem bruta são classificadas semanticamente; não adicionar/remover eventos para paridade. Estado: instrumentação B operacional e correlação explícita para logs in-span.
