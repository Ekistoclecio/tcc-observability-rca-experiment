# Stage 12 — validação integrada do experimento físico

## Objetivo

Integrar ambiente, deploy, workload, faults, coleta A/B, semântica, builders, empacotamento e finalização sem misturar validação de implementação com o dataset científico futuro.

## Fluxo integrado

O runner integrado valida exclusividade do ambiente Kind, working tree/SHA, deployment provenance, SUT e telemetria; executa as instâncias físicas em ordem fault-major/repetition-minor (`F1-R01..R0N`, depois F2..F5); para cada instância exige confirmação pelo mecanismo de controle, Ground Truth elegível, RAW A/B, semantic validation, Dataset A/B, RCA Input A/B, recuperação e cleanup. Após a última instância coleta o intervalo global e valida cobertura, builders, inventário e integridade antes de iniciar chamadas LLM.

## Publicação autocontida por run

`scripts/experiment/result_layout.py` define a única estrutura de publicação. `results/run-NNN/` contém apenas `manifest.json`, `ground-truth.json`, `raw/`, `telemetry/`, `llm/` e `evaluation/`, além do manifesto raiz `hashes.sha256`. RAW é separado por tratamento e modalidade; `telemetry`, requests model-visible e respostas são um arquivo por `fault × repetition × treatment`, com nomes `F{n}-R{nn}-{A|B}.json`. Ground Truth fica em uma árvore única `fault → repetition`; nunca é incluída no request. Evaluation fica em `evaluation/results.json`, organizada em `cases`, `faults` e `overall`.

O executor aceita `--repetitions N`; nomes, GT, contagens e agregações derivam de N, sem ramos específicos para N=1 ou N=5. O `manifest.json` registra propósito, commit testado, intervalo, protocolo/modelo e proveniência. Os arquivos de request são gravados antes de chamar o SDK e comparados com o request reconstruído pelo mesmo builder; respostas, usage, validação e latência são consolidados por diagnosis. O único hash inventory publicado é o da raiz da run; ele exclui somente a si próprio e cobre todos os demais arquivos.

Staging e inputs privados vivem fora da árvore publicada e são removidos antes da publicação. A run é montada no workspace temporário, passa por avaliação e inventário, recebe `SUCCESS` apenas após os gates finais e é movida atomicamente para o próximo `run-NNN` livre. IDs existentes nunca são sobrescritos. Falha encerra a execução e não cria uma run canônica de sucesso.

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

Usar setup limpo com versões fixadas de Kind/Kubernetes/Chaos Mesh e storage novo A/B. O setup bloqueia coexistência com outros clusters Kind relevantes no WSL devido ao limite compartilhado de inotify observado; não altera sysctls nem encerra clusters alheios. Antes da bateria, observar estabilidade idle, workload normal e consultas em janela; confirmar ausência de fault ativo, OOM/restarts inesperados e saturação não resolvida. O preflight também executa dry-run de coleta, Builder, RCA Input, montagem do novo layout e integridade sem faults nem chamadas LLM.

## Configuração e entrypoint reproduzível

`./run-experiment.sh` é o entrypoint recomendado. O shell resolve a raiz do repositório e delega ao CLI Python; não replica lógica científica. `.env.example` define somente contagens independentes de repetições F1–F5, seleção/configuração RCA, chave OpenAI e habilitação do evaluator. `0` omite a fault; valores positivos geram `R01..RN` em ordem fault-major e repetition-minor. O plano e a execução usam a mesma `ExperimentConfig`, validada antes de side effects, e a configuração não secreta fica registrada em `manifest.json`.

O loader carrega a `.env` da raiz sem sobrescrever o ambiente do processo; variáveis já exportadas têm precedência. Counts precisam ser inteiros não negativos e pelo menos uma fault deve estar ativa. Modelo, reasoning e service tier são validados contra o registry de protocolo/pricing existente; a chave é exigida somente com RCA habilitado e nunca é copiada para configuração serializável. `EVALUATION_ENABLED=true` depende de RCA habilitado. `--dry-run` imprime o plano e termina sem consultar cluster, executar faults, chamar OpenAI ou criar run.

`scripts/experiment/result_layout.py`, o evaluator e o executor aceitam contagens por fault (inclusive faults desabilitadas), sem assumir grid uniforme. O manifest guarda `fault_repetitions`; Ground Truth inclui apenas instâncias executadas, e `telemetry/`, `llm/inputs/` e `llm/responses/` usam `F{n}-R{nn}-{A|B}.json`. Agregação geral considera somente instâncias ativas. O layout e as definições métricas não mudam com a configuração.

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

## Validação estrutural

Testes offline exercitam N=1 e N=5: 5/25 instâncias GT e 10/50 IDs por tratamento/case, sem colisões; o evaluator agrega por fault, repetition, tratamento e no total. A identidade `F{n}-R{nn}` permite N maior sem alterar a estrutura. Cada run possui um único `hashes.sha256`; não há manifests aninhados publicados nem outputs de staging, control smoke, plano de execução ou protocolo duplicado. O propósito de uma execução de validação fica explícito no manifest e não deve ser interpretado como resultado definitivo do TCC.

Implementação integrada pronta para nova execução física do zero. O `docs/experiment/results/run-001/` que já está versionado é um artifact de validação da implementação/layout; não é o dataset definitivo nem deve ser interpretado como resultado generalizável. Novas execuções recebem o próximo ID livre e preservam essa run sem sobrescrevê-la.

A documentação de preparação de máquina nova e os pré-requisitos operacionais estão em `docs/experiment/README.md`. O sistema está implementado e pronto para execução configurada; esta camada foi validada offline e não inicia uma run científica por si só.
