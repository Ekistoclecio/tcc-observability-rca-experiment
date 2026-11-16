# Etapa 8 — Coleta RAW imutável

## Resultado

**PASS — coleta normal pareada A/B concluída, sem faults.** As respostas JSON nativas de Prometheus, Loki e Jaeger foram preservadas sem normalização, seleção causal ou equalização. O workload e a coleta estão registrados em [`08-raw-smoke/raw`](08-raw-smoke/raw/).

## Base e proveniência

- Branch: `experiment-v2`.
- Commit estável antes desta etapa: `7d39782a62eade3f170c017fb98da343a30ed3c6`.
- Commit do coletor indicado no manifesto: o commit base acima.
- SHA-256 do código exato do coletor executado: registrado em `manifest.json` (`collector_script_sha256`).
- As imagens de aplicação permaneceram as validadas na Etapa 7. O manifesto registra o fingerprint `5504be74e58b032d72b32a9205b7199f75680bcecea99a87301ef67e04233a82`; a validação detalhada dos dez Pods está no registro da Etapa 7.
- Cluster: `kind-tcc-experiment-v2-stage1`; nó Kubernetes `v1.35.8`. Backends fixados no ambiente: Prometheus `v3.5.0`, Loki/Promtail `3.5.5`, Jaeger `1.76.0` e OTel Collector `0.123.0`.

## Coleta

O script [`collect_raw.py`](../../../scripts/experiment/collect_raw.py) cria port-forwards temporários para os seis backends, consulta cada cenário e encerra os processos ao final. Cada corpo HTTP é gravado byte a byte. A consulta de métricas usa `query_range` com passo de 1 segundo e seletor nativo `{__name__=~".+"}`. As respostas de logs e traces permanecem separadas por serviço para preservar cada resposta do backend e seu query metadata.

Janela coletada: `2026-11-16T18:10:56.314997Z`–`2026-11-16T18:10:58.690015Z`. O coletor aguardou 20 segundos de drain antes das consultas. O workload foi executado uma vez, com dois fluxos sequenciais à taxa configurada de um fluxo por segundo: `GET /`, `GET /product/OLJCESPC7Z`, `POST /cart`, `GET /cart` e `POST /cart/checkout`. Os dez requests retornaram HTTP 200. A janela não contém fault; não havia recurso Chaos de fault nem flag F4 ativa.

O RAW resultante contém 44 respostas de backend, o registro JSON do workload, manifesto e hashes: 47 arquivos, aproximadamente 4,8 MiB. Arquivos têm permissão somente de leitura (`0444`) e diretórios `0555`. A verificação de integridade foi executada duas vezes; ambas retornaram `PASS` para os mesmos 46 arquivos listados, e o SHA-256 de `hashes.sha256` permaneceu `bd52386a9f65010e49f7f72d6e3bea3358c3de4d5b37012e6c2004ebda64e131`. O próprio `hashes.sha256` não pode ser auto-hashado; ele inclui `manifest.json` e todos os dados coletados.

## Cobertura observada

| Sinal | Scenario A | Scenario B |
|---|---:|---:|
| Séries de métricas retornadas | 217 | 1.375 |
| Streams de logs | 12 | 136 |
| Registros de log | 152 | 136 |
| IDs de trace únicos encontrados ao ler as respostas por serviço | 15 | 15 |
| Spans nas respostas por serviço (com sobreposição entre consultas) | 2.027 | 2.027 |

Os volumes nativos diferem e foram preservados. A/B não foram equalizados. As consultas Loki cobriram os dez serviços; `productcatalogservice` teve zero logs naturais nesta janela em ambos. As respostas de Jaeger são consultas por serviço e repetem traces que incluem spans de serviços diferentes: foram mantidas em cada resposta nativa, sem deduplicação pós-coleta. Não houve saturação do limite nas consultas por serviço. As respostas Loki B não têm label `filename`, coerente com a origem OTLP; uma verificação por `labels + timestamp + body` encontrou zero duplicatas exatas em A e B.

Na primeira tentativa, o endpoint Loki rejeitou `limit=10000`, acima do máximo configurado de 5.000 entradas por consulta. A divergência foi localizada na consulta ampla do coletor. A coleta final particiona por `service_name`, usa o limite suportado e falha fechada se uma partição alcançar o teto. Nenhum conteúdo foi truncado ou descartado para fazer os cenários parecerem iguais.

## Integridade e limitações

- `hashes.sha256` valida conteúdo e inventário completo e rejeita arquivos extras, arquivos alterados e caminhos ainda graváveis.
- `--verify-existing` repetido não altera o bundle.
- Prometheus e Loki foram consultados por suas APIs HTTP JSON versionadas no ambiente fixado.
- Jaeger `1.76.0` foi consultado por `/api/traces`, a API HTTP interna usada pela UI. O coletor preserva a resposta e falha se o resultado atingir o limite `10.000`; nesta janela, os resultados foram muito menores e não exigiram paginação. A API HTTP é uma limitação de compatibilidade operacional; o backend está pinado, e qualquer troca de versão deve revalidar a coleta ou migrar para QueryService gRPC estável antes da bateria final.
- Este smoke é um artefato de etapa em `stages/`; não é o RAW integral de uma bateria oficial em `results/`.

## Validação

- Preflight de ambiente: nó Ready; 11/11 Deployments do SUT disponíveis; Promtail A e backends A/B disponíveis; Collector B disponível; Promtail B ausente conforme política OTLP-only.
- Workload normal: 2/2 fluxos completos, 10/10 requests HTTP 200.
- Métricas, logs e traces: não vazios nos dois cenários.
- Coleta: 44 respostas nativas registradas; nenhuma consulta atingiu o limite de segurança.
- Integridade: `PASS` em duas leituras consecutivas; hashes idênticos.
- Testes locais do coletor: 5 testes focados passaram; `py_compile` e `git diff --check` passaram.
- Sem faults, Chaos, Dataset Builder ou RCA Input.

## Critérios de PASS

| Critério | Resultado |
|---|---|
| RAW A/B nativo, separado e não equalizado | PASS |
| Métricas, logs e traces não vazios nos dois cenários | PASS |
| Janela e configuração do workload registradas | PASS |
| Query metadata, manifesto e hashes presentes | PASS |
| Respostas/traces preservados sem deduplicação semântica | PASS |
| Bundle read-only e integridade repetível | PASS |
| Fault/Chaos ausente | PASS |

## Próxima etapa

A Etapa 8 está concluída e commitada junto do coletor e deste registro. A próxima é a Etapa 9: validação semântica diretamente sobre RAW, incluindo smokes F4/F5 autorizadas pelo plano. Nenhum fault foi executado nesta etapa.

## Errata de proveniência dos traces A (registrada durante a Etapa 9)

Uma auditoria posterior encontrou uma colisão no coletor original. Ele usava a porta local fixa `16686` para Jaeger A e escondia a saída do processo `kubectl port-forward`. Já havia um port-forward de Jaeger B escutando em `127.0.0.1:16686`; a verificação de readiness consultava essa porta ocupada e não provava qual serviço respondia. Assim, as consultas de traces rotuladas A no bundle original foram atendidas por Jaeger B. Os arquivos originais permanecem inalterados e seu conteúdo de traces A não deve ser usado como evidência.

A causa foi corrigida no commit `265aa87039964a412773a9046b7bba65f587c76f`: cada forward agora reserva uma porta efêmera, precisa confirmar em sua própria saída a associação local/remota esperada e permanece vivo durante a coleta. A mesma janela normal, `2026-11-16T18:10:56.314997Z`–`2026-11-16T18:10:58.690015Z`, foi reconsultada sem gerar requests adicionais. O bundle corrigido está em [`08-raw-smoke/raw-corrected`](08-raw-smoke/raw-corrected/), com hashes próprios. Ele registra 310 spans únicos/12 trace IDs A e 317 spans únicos/15 trace IDs B; as contagens anteriores de traces A/B iguais não são válidas. Métricas e logs foram reconsultados; os counts permaneceram em 217/1.375 séries e 152/136 registros, respectivamente. A consulta corrigida usou `drain_seconds=0`, pois foi uma leitura histórica de dados já ingeridos, não uma nova coleta imediatamente após workload.

O incidente, a evidência de colisão e o alcance da invalidação estão em [`reports/INCIDENT-08-jaeger-port-forward-collision.md`](../reports/INCIDENT-08-jaeger-port-forward-collision.md). O bundle original foi preservado; a Etapa 9 usa somente a cópia corrigida para comparação semântica.
