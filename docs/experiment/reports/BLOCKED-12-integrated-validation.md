# BLOCKED — Etapa 12: validação integrada F1–F5

## Estado

- Branch: `experiment-v2`
- Último commit estável antes da correção: `e66b8160b38e6bdedf7c6e246fc0a4f7d290852d`
- Resultado da bateria: inválido e interrompido em F1. Nenhuma conclusão F2–F5 foi produzida por esta tentativa.
- Evidências preservadas: [`BLOCKED-12-artifacts-1794863260/`](BLOCKED-12-artifacts-1794863260/)

## Observado

F1 confirmou CPU elevada em checkoutservice nos dois cenários, mas o gate de duração não encontrou um baseline numérico válido e superior para comparar. Os artefatos da tentativa preservada mostram que a janela normal começava antes do fluxo de warm-up. A primeira amostra da duração de checkoutservice aparece aproximadamente cinco segundos após o início da janela, depois de a aplicação já ter processado a primeira operação. O Builder marcou a duração como `missing_baseline`, conforme o contrato temporal.

Os dados B também contêm séries cumulativas de checkoutservice que diminuem dentro da janela (por exemplo, o contador `rpc.server.call.duration` de PlaceOrder). A configuração do Collector B conserva streams inativos por cinco minutos. Os recursos de alguns SDKs não incluem identidade de instância: para esses serviços as séries exportadas usam somente `service.name`; a substituição de Pod pode, portanto, juntar uma série antiga conservada e uma série nova inicializada em zero sob o mesmo conjunto de labels. A mesma queda foi observada em duas janelas normais preservadas. As amostras .NET e Java possuem `exported_instance`; as amostras Go, Node.js e Python analisadas não possuíam identidade equivalente. Os recursos OTel dessas implementações confirmam que definem explicitamente `service.name` e omitem `service.instance.id`.

## Divergência e causa

1. **Baseline:** o runner usa o `started_at` do processo de workload, registrado antes do warm-up, como início do Dataset normal. Essa janela não garante que os counters de duração já tenham sido inicializados e observados pelos dois Prometheus.
2. **Identidade temporal em B:** recursos OTel de Go, Node.js e Python definem `service.name`, mas não definem `service.instance.id`. O Prometheus exporter recebe streams com as mesmas labels após substituição de Pods; com retenção de métricas inativas, valores de processos diferentes podem colidir e parecer reset.

As duas causas são bugs de medição/identidade na origem e no controle temporal. Não são justificativa para converter baseline ausente/reset em zero ou relaxar o gate F1. Ground Truth e semântica do fault não são alterados. A janela F1 desta tentativa durou 19,0327 segundos e o CPU de checkoutservice subiu de aproximadamente 0,0096 para 0,153 cores em A/B; portanto a evidência de CPU passou e a divergência ficou restrita ao sinal de duração usado no gate.

## Invariante afetado

Counters cumulativos devem ter baseline observado para a janela e não podem atravessar um reset/uma fronteira de instância como se fossem a mesma série. Baseline ausente e reset permanecem `UNKNOWN`.

## Correção mínima recomendada

- Dar a cada Pod uma identidade real e estável durante sua vida via Downward API (`metadata.uid`) e incluir essa identidade como `service.instance.id` nos recursos OTel dos serviços Go, Node.js e Python. Manter a identidade nativa no RAW; não usar Ground Truth ou a causa correta para selecionar séries.
- Separar o fluxo de warm-up da janela medida. Após o warm-up, aguardar amostras recentes de count e sum de PlaceOrder nos Prometheus A e B; iniciar então a janela de workload medida. Registrar ambas as fases no artefato de controle.

Essa correção preserva a execução física compartilhada, o workload e a instrumentação A/B; corrige a identidade da série e delimita a medição antes de recolher evidência.

## O que não fazer

- Não enfraquecer `counter_delta`, nem converter `missing_baseline`/`counter_reset` em zero.
- Não derivar métricas de traces, não equalizar contagens e não filtrar por fault/serviço correto.
- Não alterar a duração/semântica de F1 para fazer o gate passar.

## Correção implementada e próximas validações

A correção local separa warm-up da janela medida e aguarda amostras frescas de count e sum de PlaceOrder nos Prometheus A e B antes de iniciar a medição. Recursos OTel de Go, Node.js e Python agora incluem `service.instance.id` derivado do UID real do Pod, propagado via Downward API. As regras de deltas e resets do Builder não foram alteradas. Os testes focados passaram; build/deploy do commit corrigido, proveniência e validação normal ainda são gates pendentes. Depois deles, a bateria inválida não poderá ser retomada ou mesclada: a validação integrada deve recomeçar desde F1 em uma execução nova.
