# Stage 08 — coleta RAW

## Objetivo

Capturar dados nativos de todas as fontes no intervalo oficial e preservar proveniência, cobertura, limites de backend e hashes sem projeção para RCA.

## Implementação

`scripts/experiment/collect_raw.py` coordena coletores de métricas, logs A/B, traces A/B e control. Durante execução, bundles temporários contêm payloads nativos, manifesto temporal/de fonte e hashes; control inclui workload e confirmações do plano de controle. Na publicação da run, a telemetria nativa global é organizada em `raw/A/{metrics,logs,traces}` e `raw/B/{metrics,logs,traces}`. RAW não é filtrado para igualar A/B, não é reescrito para acomodar Builder e não recebe Ground Truth model-visible.

Loki usa boundaries integer-ns exatas, intervalos `[start,end)`, janelas-base de 30 s e paginação `forward`, `limit=5000`, split recursivo determinístico e fail-closed em saturação irredutível. Selector B usa labels reais (`service_name`); não presume `namespace` ausente.

Jaeger usa Badger persistente em PVC separado por cenário, limite observado de 512 MiB, queries HTTP JSON em janelas de 30 s, `limit=10000`, split recursivo e deduplicação lógica. Chunks nativos são preservados; queries monolíticas não são usadas.

## Integridade, validação e problemas consolidados

Comparar boundaries em integer ns (delta início/fim zero), cobertura sem gaps/overlap, split completo, restart/OOM, manifests e SHA-256. `control/workload.json` permanece obrigatório na coleta temporária e seus fatos de auditoria necessários são consolidados no `manifest.json` e `ground-truth.json` publicados. A publicação copia os bytes de resposta nativos por modalidade e verifica o inventário externo da run. Queries grandes podem saturar; split recursivo conserva intervalo. Collector B não pode depender de labels A. A run final contém um único `hashes.sha256` na raiz, cobrindo todo arquivo publicado exceto a si mesmo.

## Escala por repetição

O orquestrador executa fault-major e repetition-minor (`F1-R01..R0N`, depois F2..F5), com a mesma janela física derivando os tratamentos A e B. O RAW publicado cobre o intervalo completo da run por tratamento; chunks nativos podem existir dentro de cada diretório de modalidade. O layout não depende de N.

## Estado final

Coleta implementada para produzir RAW completo e nativo no layout autocontido da run; artifacts de coletas de desenvolvimento não são mantidos.
