# Stage 08 — coleta RAW

## Objetivo

Capturar dados nativos de todas as fontes no intervalo oficial e preservar proveniência, cobertura, limites de backend e hashes sem projeção para RCA.

## Implementação

`scripts/experiment/collect_raw.py` coordena coletores de métricas, logs A/B, traces A/B e control. Bundles contêm payloads nativos, manifesto temporal/de fonte e hashes; control inclui workload e confirmações do plano de controle. RAW não é filtrado para igualar A/B, não é reescrito para acomodar Builder e não recebe Ground Truth model-visible.

Loki usa boundaries integer-ns exatas, intervalos `[start,end)`, janelas-base de 30 s e paginação `forward`, `limit=5000`, split recursivo determinístico e fail-closed em saturação irredutível. Selector B usa labels reais (`service_name`); não presume `namespace` ausente.

Jaeger usa Badger persistente em PVC separado por cenário, limite observado de 512 MiB, queries HTTP JSON em janelas de 30 s, `limit=10000`, split recursivo e deduplicação lógica. Chunks nativos são preservados; queries monolíticas não são usadas.

## Integridade, validação e problemas consolidados

Comparar boundaries em integer ns (delta início/fim zero), cobertura sem gaps/overlap, split completo, restart/OOM, manifests e SHA-256. `control/workload.json` é obrigatório. Staging copia bundles integralmente e valida origem/destino. Queries grandes podem saturar; split recursivo conserva intervalo. Collector B não pode depender de labels A. Estado: coleta RAW nativa e auditável implementada.
