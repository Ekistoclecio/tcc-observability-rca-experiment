# Etapa 12O — correlação do frontend e validação da coleta RAW

## Estado

**PASS — gates diagnósticos de correlação, cobertura Loki, coleta RAW e staging passaram.** Não foram executados faults e esta etapa não é uma bateria experimental oficial. O commit-base durante a validação foi `47f45481e0df741c476f77fea4278a1d66e874d8`; as alterações ainda estão locais e precisam ser consolidadas antes de uma bateria.

## Correlação de logs do frontend B

A Etapa 12N provou que o `logrus.Entry` era criado antes de `otelmux` criar o SERVER span e depois era reutilizado pelos handlers. A menor correção manteve a ordem dos middlewares e as chamadas/mensagens naturais: o frontend obtém o `Entry` existente e aplica `WithContext(r.Context())` no momento de uso pelo handler. Não são adicionados IDs manualmente. Logs sem span ativo continuam sem IDs.

O teste Go usa a bridge OTel Logs e um exporter de captura. Com span ativo, os IDs exportados são exatamente os do contexto; sem span, nenhum ID é fabricado. `go test ./...` passou em container `golang:1.27.1`.

Em workload normal diagnóstico, sem faults, **33/33** logs naturais de handler dentro de SERVER spans carregaram `trace_id` e `span_id` que coincidiram com o span real. Os logs `request started`/`request complete` fora do span permaneceram sem IDs. Para a mesma execução, os eventos request-scoped A/B corresponderam; quatro avisos `fmt.Println` presentes somente em A continuam classificados como `RAW_ONLY_DIFFERENCE`, sem perda de cobertura diagnóstica. A igualdade de contagem bruta não é requisito.

## Estratégia Loki

A validação fresca usou Loki `3.5.5`, somente aritmética inteira em nanossegundos, limites efetivamente enviados como strings decimais e sem conversão intermediária por float. O intervalo foi `[1794916908287200542,1794916974416306773)`, com deltas inicial/final de `0 ns`. Janelas de 30s e 60s foram comparadas por multiset de labels canônicas, timestamp inteiro e linha literal para os dez serviços em A e B: todos os 20 pares produziram multiset igual entre estratégias. Todas as respostas foram HTTP 200, sem leaves saturadas e sem gaps/overlaps.

A ausência anterior aparente de logs B/frontend era um seletor incorreto: Loki B não possui label `namespace`; o seletor válido é `{service_name="<service>"}`. O pipeline de logs B estava ingerindo dados. O erro de arredondamento de timestamp do harness anterior foi corrigido na validação, sem alterar a ingestão.

Resumo da comparação: 30s produziu 60 queries e 60 leaves, máximo de 322 entradas/278.044 bytes; 60s produziu 40 queries e 40 leaves, máximo de 630 entradas/539.260 bytes. Não foi necessário split neste volume. A semântica Loki `[start,end)` foi validada e a implementação mantém split recursivo inteiro no midpoint quando uma resposta alcança `limit=5000`; a leaf mínima saturada falha fechada.

## Jaeger e Badger

Mantida a configuração já aprovada: Jaeger `1.76.0`, Badger persistente, PVCs separados para A/B, configuração simétrica, TTL explícito de `168h` e limite de memória de `512 MiB`. A coleta usa janelas inclusivas de 30s, `limit=10000`, split determinístico quando há saturação e chunks JSON nativos. Persistência após recriação de Pod/SIGKILL/restart por OOM já havia sido validada nas etapas diagnósticas anteriores.

## Coleta RAW integrada diagnóstica

Foi executado workload normal sem fault com intervalo Loki exato `[1794917990370418088,1794917996399264387)`. O manifesto preservou ambos os limites com delta `0 ns` e fração RFC3339 de nove dígitos. A coleta do collector passou para métricas, logs A/B, traces A/B e `control/workload.json`. Bundle: **46 arquivos verificados**; A: 306 entradas de log e 244 séries; B: 274 entradas de log e 1.681 séries. O Builder leu os chunks e passou (1.017 fatos A, 973 B). O staging validou os 46 hashes e preservou `control/workload.json`.

Na observação pós-coleta, Jaeger A/B estavam Ready, com restartCounts estáveis em `1` e `2`, e sob o limite de `512 MiB` (cgroup: aproximadamente 119 MiB e 248 MiB); `memory.events` não indicou OOM. Os PVCs A/B estavam Bound. `kubectl top`/Metrics API não estava disponível; cgroup foi a fonte de memória. Nenhum fault foi executado.

## Implementação RAW Loki

O collector agora usa janelas `[start_ns,end_ns)` de 30s, `direction=forward`, limite 5000 e split recursivo determinístico. Respostas leaf são gravadas byte a byte em `dataset{A,B}/logs/<service>/chunk-*.json`; hashes cobrem os bytes originais. Queries saturadas são preservadas somente como artifacts de controle não model-visible. O manifest registra serviço/cenário, limites enviados, direção, contagens, tamanho, SHA-256, profundidade de split, duração e status, e valida cobertura de todos os serviços/cenários.

O Builder, o validador semântico e a geração de amostras de auditoria leem a estrutura por chunks sem modificar a semântica de normalização. A proveniência liga cada fato ao arquivo RAW e ao respectivo hash. A correção anterior de empacotamento permanece: `global-raw/control/` é copiado recursivamente para staging.

## Testes e limites

- Testes Python experiment-v2: **66 passaram**; `py_compile` passou.
- Testes do frontend: `go test ./...` passou.
- Kustomize de `experiment-v2`, `observability-a` e `observability-b`: renderização passou usando `LoadRestrictionsNone` porque os overlays referenciam a base fora do diretório.
- JSON de faults/workload/schemas e `git diff --check`: passaram.
- A validação de logs prova a cobertura do que está disponível/indexado no Loki dentro do intervalo consultado; não afirma igualdade automática entre logs emitidos pela aplicação e logs ingeridos.
- A imagem do frontend usada no teste runtime de correlação era uma imagem diagnóstica local de working tree suja; não constitui provenance nem tested commit.

## Bateria oficial e próximo gate

F1–F5 **não iniciados**. O próximo fluxo é organizar as mudanças locais e remoções intencionais de reports temporários, executar os gates completos, criar um commit limpo, construir/deployar a partir do novo SHA e validar provenance/preflight. Somente então a bateria oficial será iniciada desde F1. Resultados de todas as baterias anteriores permanecem inválidos; nenhum resultado foi publicado em `results/` ou `teste/` nesta etapa.
