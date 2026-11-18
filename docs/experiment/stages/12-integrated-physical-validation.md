# Stage 12 — validação integrada do experimento físico

## Objetivo

Integrar ambiente, deploy, workload, faults, coleta A/B, semântica, builders, empacotamento e finalização sem misturar validação de implementação com o dataset científico futuro.

## Fluxo integrado

O runner integrado valida exclusividade do ambiente Kind, working tree/SHA, deployment provenance, SUT e telemetria; executa F1–F5 sequencialmente; para cada fault exige confirmação pelo mecanismo de controle, Ground Truth elegível, RAW A/B, semantic validation, Dataset A/B, RCA Input A/B, recuperação e cleanup. Após F5 coleta o intervalo global e verifica manifests, hashes, staging, pacote de auditoria e publicação. Qualquer gate falho bloqueia a etapa seguinte e invalida a execução correspondente.

## Decisões e correções consolidadas

### F2 — targeting dinâmico

O destino de `checkoutservice → paymentservice` é derivado do ClusterIP atual de `paymentservice`; Chaos Mesh recebe `externalTargets=<ClusterIP>:50051`, `direction=to` e 100 ms. A confirmação causal é feita por recursos/seleção do plano de controle e configuração de rede, não por spans.

### Loki — cobertura temporal exata

As janelas são `[start,end)` em integer nanoseconds, alinhadas sem arredondamento, com janelas-base de 30 s e direção `forward`. A consulta usa limit 5000 e split recursivo determinístico quando satura; saturação irredutível em 1 ns falha fechada. Leaves devem cobrir o intervalo sem lacunas ou sobreposição e preservar chunks/hash. Scenario B seleciona labels realmente existentes (`service_name`); não adiciona `namespace` quando o stream B não o contém.

### Jaeger — persistência e extração

A/B usa Jaeger 1.76 com Badger em PVCs separados. O limite de memória configurado é 512 MiB e deve ser monitorado, sem mascarar OOM. A coleta usa janelas HTTP JSON de 30 s, limit 10000, split recursivo determinístico em saturação e deduplicação lógica de trace/span. Chunks nativos e hashes são preservados; queries monolíticas não são usadas.

### Correlação dos logs B

O handler reutilizava uma `logrus.Entry` criada antes de `otelmux` iniciar o SERVER span. A correção localizada atualiza a entry com o contexto da request dentro do handler; logs naturais emitidos sob span ativo carregam `trace_id`/`span_id` reais. Logs fora de span continuam sem IDs inventados. A contagem bruta de logs não é equalizada; só facts semânticos diagnósticos são comparados.

### Comparação temporal e inventário

O semantic validator converte timestamps RFC3339 válidos para instante absoluto exato em integer nanoseconds, inclusive offsets equivalentes; não compara strings, não usa float nem tolerância. O inventário externo exclui somente seu próprio `hashes.sha256` de raiz e inclui manifests de hash aninhados como artifacts normais.

### Finalização de temporários

RAW temporário pode manter diretórios read-only. Antes da remoção, a rotina prepara permissões somente nos diretórios da cópia descartável e faz uma remoção única; não altera bytes/modos publicados, não tenta novamente e repropaga falhas. Diagnósticos preservam traceback/contexto do filesystem. O estado `SUCCESS` só é gravado depois de todos os gates, publicação e integridade final.

### Empacotamento global

O bundle global contém metrics, logs A/B, traces A/B e control, com `control/workload.json`, manifests e hashes. Staging copia bundles integralmente; a verificação compara arquivos físicos com o inventário, incluindo manifests internos, e confirma os hashes de source e staging.

## Validação operacional

Usar setup limpo com versões fixadas de Kind/Kubernetes/Chaos Mesh e storage novo A/B. O setup bloqueia coexistência com outros clusters Kind relevantes no WSL devido ao limite compartilhado de inotify observado; não altera sysctls nem encerra clusters alheios. Antes da bateria, observar estabilidade idle, workload normal e consultas em janela; confirmar ausência de fault ativo, OOM/restarts inesperados e saturação não resolvida.

## Problemas relevantes e solução final

- F2 originalmente dependia de endpoint fixo; substituído por descoberta dinâmica de ClusterIP e target externo específico.
- Limites temporais representados como strings causavam falso mismatch; comparação exata por instante resolveu sem modificar RAW/GT/manifest.
- O inventário excluía todo arquivo chamado `hashes.sha256`; agora exclui apenas o manifesto raiz.
- `rmtree` de árvore RAW read-only causava EACCES no workspace descartável; permissões de diretório são preparadas antes da remoção única.
- Estado `SUCCESS` prematuro foi movido conceitualmente para após finalização completa.
- OOM em Jaeger deve ser diagnosticado em storage/runtime limpos; não é resolvido automaticamente aumentando memória.

## Componentes principais

`scripts/experiment/run_integrated_validation.py`, `fault_smoke.py`, `collect_raw.py`, `validate_raw_semantics.py`, `build_rca_input.py`, módulos `environment/`, `observability-a/`, `observability-b/`, `jaeger/`, `loki/`, `finalization/`, manifests `kustomize/experiment-v2/` e testes `scripts/experiment/` / `tests/experiment/`.

## Estado final

Implementação integrada pronta para nova execução física do zero. Validações e baterias anteriores foram apenas desenvolvimento e não permanecem como dataset, resultado ou auditoria no working tree.
