# Common Observability Contract v1 — pós-implementação

Validação técnica local, não científica. RAW global em `audit/results/manifest.json`; comparação de conceitos calculada sem seleção por Ground Truth.

## Estado dos dados RAW

| Cenário | Metric samples | Logs | Traces únicos | Spans únicos | Bytes RAW (3 sinais) |
|---|---:|---:|---:|---:|---:|
| A | 9,574 | 6,419 | 413 | 8,919 | 15,914,553 |
| B | 67,986 | 6,268 | 355 | 8,861 | 46,522,815 |

As contagens A/B seguem diferentes porque os backends e SDKs produzem snapshots nativos distintos e as coletas de Prometheus/Jaeger são assíncronas. Elas não são usadas como critério de igualdade. RAW permanece integral e sem compactação.

## Paridade dos conceitos observados

| Sinal | A-only diagnostic concepts | B-only diagnostic concepts | Resultado |
|---|---:|---:|---|
| Metrics (request/count, duration, CPU) | 0 | 0 | PASS |
| Application log events | 0 | 0 | PASS |
| Logical spans | 0 | 0 | PASS |

Os nomes de métricas nativos diferem entre A e B por construção. O RCA Dataset usa o mapeamento fixo para `request_count`, `request_duration` e `cpu_utilization`; os nomes nativos permanecem como proveniência. Métricas de runtime/SDK/exporter B foram restringidas no Collector e não entram na view comum.

A identificação por serviço/operação/edge/kind/status e a lista de eventos de aplicação coincidem no inventário atual. `trace_id`, `span_id`, parent context e recursos semânticos nativos de B permanecem diferenças estruturais/correlação permitidas. Nenhuma correlação foi sintetizada em A.

## CPU

A e B derivam CPU dos mesmos contadores cAdvisor `container_cpu_usage_seconds_total`, com scrapes independentes. Os RCA datasets contêm `cpu_utilization` para os 11 componentes do allowlist em cada smoke. A unidade é CPU cores, não porcentagem do limite do container. Os valores são amostras em janelas separadas e não devem ser tratados como medições temporalmente idênticas.

## Limpeza e semântica temporal

- Request counters/duration counters: deltas entre amostras disponíveis na janela; resets são somados a partir do valor pós-reset e marcados. Uma série cuja primeira amostra ocorre depois do início da janela não possui baseline válido e fica `null`/`missing_baseline`, em vez de ser tratada como zero.
- Histograms: buckets não são enviados. Count/sum delta e média em segundos são preservados.
- CPU gauges: first/min/max/avg/last determinísticos.
- SDK/resource/process/runtime metadata, stacktrace de span e ruído de health/scrape/exporter não entram no RCA view.
- Logs de aplicação não são filtrados por severidade. Eventos repetidos em timestamps distintos são mantidos.

## Validação das execuções

Executadas somente F1, F4 e F5, uma run nova cada. F2/F3 não foram executadas nesta verificação.

| Fault | A metrics/logs/traces/spans | B metrics/logs/traces/spans | Resultado de recuperação |
|---|---|---|---|
| F1 | 97/1933/110/2794 | 97/1928/110/2794 | Ground Truth valid; injection/recovery confirmed |
| F4 | 107/1951/111/2707 | 107/1890/111/2707 | Ground Truth valid; injection/recovery confirmed |
| F5 | 117/1743/96/2374 | 117/1668/96/2374 | Ground Truth valid; injection/recovery confirmed |


## Anomalia de provenance corrigida

Na união global de respostas Jaeger, IDs curtos de processo (`p1`, `p2`, ...) eram reutilizados por respostas independentes. O merge anterior combinava os mapas `processes` e podia atribuir um span ao serviço errado. O merge agora cria IDs determinísticos baseados nos metadados de processo antes da união; o normalizador por janela também mantém o nome do serviço junto de cada span. O RAW Jaeger global foi recolhido novamente da mesma janela e a paridade lógica foi recalculada. Os snapshots RAW de Prometheus/Loki não foram alterados nesta correção.

Este defeito explica a divergência aparente `emailservice|ProductCatalogService/*` que apareceu na primeira auditoria pós-run; não era evidência de spans lógicos diferentes no SUT.

## Limitações

- Scrapes isolados têm jitter; CPU A/B usa o mesmo tipo de contador, mas não exatamente o mesmo instante de scrape.
- Counter delta usa amostras retornadas na janela; série com menos de duas amostras ou primeira amostra posterior ao início da janela fica sem baseline explícito. Não há extrapolação inventada.
- A comparação abrange apenas F1/F4/F5 smoke e não prova comportamento nas F2/F3 nem em cargas oficiais.
- As diferenças de tamanho e estrutura não são uma conclusão sobre qualidade diagnóstica.

## Anomalia de valores métricos F4 — causa identificada

Na run F4, os dois datasets de traces contêm os mesmos resultados `paymentservice / Charge`: 17 spans de sucesso e 5 spans com falha. Os request counters não concordam: A apresenta delta de sucesso 16 e B delta de sucesso 7. A série de erros aparece depois do início da janela RCA e não possui baseline válido em nenhum backend; após a correção conservadora do baseline, ambos os deltas de erro são `null` com `delta_status=missing_baseline` (os RAWs mantêm os valores cumulativos). A investigação focalizada posterior identificou uma combinação de avaliação temporal do Builder e atraso de publicação/coleta B. O query_range de 5 s não avaliava o scrape B mais recente ainda dentro da janela; com passo comum de 1 s, o mesmo histórico resulta em A=16/B=13. Na borda exata, os valores disponíveis eram A=43 e B=40, diferença explicada pelo leitor OTel periódico de 5 s seguido do scrape Prometheus de 5 s. O Builder agora usa `step=1s` em A e B. A semântica de incremento é equivalente; permanece uma limitação temporal explícita de coleta, não mascarada por traces. O delta de erro continua `null / missing_baseline`. Consulte `f4-metric-investigation.md` e `f4-metric-value-anomaly.json` para séries, labels, timestamps e respostas Prometheus.

Os hashes e os paths concretos que sustentam a discrepância estão em `f4-metric-value-anomaly.json`.

## Ajuste local do ambiente

Antes das smokes, o `emailservice` encerrava com código 137 no limite local de memória `128Mi`. O overlay experimental local elevou o limite para `256Mi` (request mantido em `128Mi`) para manter os 11 serviços Ready durante a validação. Esse ajuste de recurso não altera a lógica ou a instrumentação dos sinais; está versionado separadamente e deve ser mantido como parte da configuração local usada para reproduzir esta validação.
