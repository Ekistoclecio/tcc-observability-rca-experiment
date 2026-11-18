# Stage 03 — Ground Truth independente

## Objetivo

Registrar mecanismo, localização e janela da fault por evidência de controle, sem inferência de métricas/logs/traces.

## Implementação

`config/experiment/ground-truth.schema.json` define o contrato e `scripts/experiment/ground_truth.py` valida transições e persiste JSON atomicamente. Campos incluem fault/type, localização tipada, início pedido/confirmado, fim confirmado, evidência de injeção/recuperação, elegibilidade e status.

Máquina de estados: `PENDING → ACTIVE → RECOVERED`; apenas recovered com confirmações válidas é elegível. Falhas de confirmação podem terminar inelegíveis. Timestamps não podem retroceder e transições fora de ordem são rejeitadas. Localizações usam tipos `service`, `service_edge`, `operation` e `datastore`.

## Invariantes e validação

Ground Truth é obtida do plano de controle específico (Chaos Mesh, réplicas/endpoints ou flag de aplicação), não do comportamento observado pela telemetria. Testes de schema, round-trip, ordenação temporal e transições usam fixtures artificiais. Estado: contrato independente pronto para consumo pelo orquestrador de faults.
