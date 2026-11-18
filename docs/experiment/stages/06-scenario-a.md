# Stage 06 — Scenario A: instrumentação convencional

## Objetivo

Implementar baseline convencional com métricas, logs e traces, preservando independência dos sinais e sem criar correlação log↔trace artificial.

## Implementação

Manifests em `scripts/experiment/observability-a/` e `kustomize/experiment-v2/` implantam Prometheus, Promtail/Loki e Jaeger. Métricas convencionais de aplicação são coletadas por scrape; cAdvisor fornece utilização de CPU do SUT. Promtail coleta stdout CRI e Loki preserva labels/stream. Traces convencionais chegam a Jaeger pelo endpoint Zipkin. A propagação customizada de tracing A permanece dentro de sua própria instrumentação.

Logs A permanecem stdout convencionais sem adição artificial de `trace_id`/`span_id` para paridade com B. Health/lifecycle e demais logs naturais permanecem no RAW.

## Decisões e limites

cAdvisor pode expor medidas agregadas de Pod e container; não somar famílias que representam o mesmo recurso como medidas independentes. Exportação Zipkin tem resolução inferior à de SDKs e spans muito curtos podem aparecer com duração zero; isso é limitação da fonte, não motivo para reescrever RAW. Contagem de logs A/B não é meta de igualdade.

## Validação

Render Kustomize e verificar targets/séries, logs reais e operações Jaeger sob workload normal sem fault. Estado: stack A independente e operacional.
