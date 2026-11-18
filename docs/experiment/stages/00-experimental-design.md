# Stage 00 — desenho experimental e invariantes

## Objetivo

Definir a comparação controlada de qualidade/contextualização/correlação da telemetria em RCA de microsserviços. O documento normativo completo é [`experiment-design.md`](../experiment-design.md); este stage registra como o desenho orienta a implementação.

## Implementação

O Online Boutique é submetido a um único workload determinístico e a uma fault por execução física. Scenario A (instrumentação convencional) e Scenario B (OpenTelemetry) observam simultaneamente a mesma aplicação e os mesmos eventos. Os sinais disponíveis nos dois tratamentos são métricas, logs e traces; a variável independente é a estrutura semântica e a correlação, não a presença/ausência de uma modalidade.

As cinco classes planejadas são `cpu_saturation`, `network_latency`, `service_unavailable`, `application_exception` e `datastore_unavailable`. A unidade pareada é a execução física: uma Ground Truth independente e os dois RCA Inputs derivados de A/B. Ground Truth é produzida pelo mecanismo de controle, nunca inferida da telemetria.

## Decisões metodológicas

- A/B deve compartilhar execução física, workload, fault e janela temporal.
- Os sinais RAW são preservados sem normalização destinada a igualar volumes ou favorecer um tratamento.
- A diferença de estrutura/cobertura que resulta da instrumentação é parte do tratamento; igualdade de contagens brutas não é requisito.
- Fault confirmation e Ground Truth usam plano de controle; traces não confirmam causalmente a injeção.
- O modelo recebe somente RCA Input. Ground Truth, tratamento, ID de fault, caminhos e dados de Chaos/Kubernetes permanecem fora do request.
- Os pilotos de implementação não são resultados científicos. O estado limpo do repositório não contém dataset nem predictions anteriores.

## Validação

Antes de uma execução nova, seguir Stage 12: ambiente exclusivo e limpo, provenance, saúde do SUT e backends, cobertura temporal, validação semântica, integridade de artifacts e finalização. A execução inferencial só começa após protocolo, runner e hashes estarem congelados.

## Invariantes

A mesma execução física alimenta A e B; Ground Truth permanece independente da telemetria; a projeção RAW→Dataset→RCA Input é auditável; nenhuma paridade é fabricada após coleta. A implementação está pronta para uma nova execução, mas **nenhum resultado experimental está armazenado**.
