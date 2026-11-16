# Etapa 7 — Scenario B OpenTelemetry

## Resultado

**PASS — validação normal runtime concluída, sem fault.** A etapa adiciona OTel Logs por integração idiomática a cada linguagem. Uma chamada natural existente de logger alimenta stdout convencional para A e um `LogRecord` OTLP separado para B. Não foi introduzida uma camada `Shared Observation` nem uma segunda chamada manual por evento.

## Base e proveniência da validação

- Branch: `experiment-v2`.
- Commit base estável: `fdbd151c4e47fc561cd87150ac9e666ef27a5f33`.
- Validação: working tree pre-commit, fingerprint SHA-256 `5504be74e58b032d72b32a9205b7199f75680bcecea99a87301ef67e04233a82`; tag local `stage7-precommit-5504be74e58b`.
- As dez imagens receberam label `org.opencontainers.image.revision=PRE_COMMIT_STAGE7_<fingerprint>`. Para cada serviço, o digest do config local, o config digest no containerd do node Kind e o `imageID` do Pod coincidiram. Os dez Pods usaram a mesma tag/fingerprint; o SUT estava `11/11 Ready` incluindo `redis-cart`.
- O fingerprint é uma identificação da árvore pre-commit, não um SHA Git. Esta validação comprova a imagem executada contra o snapshot local usado no build; não é uma bateria oficial nem substitui a provenance Git das etapas congeladas.

| Serviço | Pod validado | Runtime config digest |
|---|---|---|
| `frontend` | `frontend-656bb976f6-pp842` | `sha256:6273502718d14344db3097fec8d520349e6c8c28035e04cda7d120018066202f` |
| `checkoutservice` | `checkoutservice-589bf57dfb-wt7wp` | `sha256:f7401922cd6ed78cb8909458a3f8916d84dd53a02ebb53d1b37747c6cd9e3146` |
| `cartservice` | `cartservice-867dd54ccc-vffw9` | `sha256:87172c23d20665c50850b2c105031bf232983bba784ca64864cfc29d86331f57` |
| `productcatalogservice` | `productcatalogservice-5b4d54f7fd-c8skb` | `sha256:832b4b86c74511862cc8139f32c039f822655953c22f8c837cd77ae6b84b5c7b` |
| `currencyservice` | `currencyservice-588ff45f9b-hlx2p` | `sha256:b6f1e57e4da0b4c171fdd59076dfa2559bf44df36ff993f3d69de3c9c2936a81` |
| `paymentservice` | `paymentservice-6d64f4bff9-b9m9n` | `sha256:b09e992abfe59eee257dc90756807d7ab8de5b331b6d0a1f3b82fc356c5d699c` |
| `shippingservice` | `shippingservice-576ff8ff64-srt8b` | `sha256:a466d9558990197b9537ba8dc17284c9f732bf1617736893d7d4cdb655885654` |
| `emailservice` | `emailservice-5bf4c8b7b8-ddhh8` | `sha256:e8c46d6c045bdc700ec107c4cdb61e01c6bde38bf674ba9e5efee4214a691b04` |
| `recommendationservice` | `recommendationservice-7746489896-zz7pw` | `sha256:4fce2231d2c13d02a0419333afbd8de5952dfd5f7c2bd6117403013ed3be1c6a` |
| `adservice` | `adservice-75546894bb-dxrsn` | `sha256:77d6465e5b5d2c337cbfc55baefb61b895b3f8b6d88249995bb389a61cdcedbb` |

## Serviços e integração de logging

| Linguagem/serviços | Integração B | Caminho A |
|---|---|---|
| Go — `frontend`, `checkoutservice`, `productcatalogservice`, `shippingservice` | Bridge `otellogrus`; hook no Logrus; entries dos handlers recebem o contexto da operação quando há log natural | Logrus JSON em stdout |
| Node.js — `currencyservice`, `paymentservice` | Pino instrumentation `0.68.0`, OTel Node SDK `0.222.0`, OTLP Logs exporter; instrumentation registrada antes de carregar o logger Pino | Pino stdout sem mixin de trace/span (`disableLogCorrelation`) |
| .NET — `cartservice` | `ILogger` com provider OpenTelemetry `1.21.0`; `Activity.Current` alimenta correlação | Provider Console/stdout do `ILogger` |
| Python — `emailservice`, `recommendationservice` | Python `LoggingHandler`, `LoggerProvider` e OTLP exporter | Handler stdout existente |
| Java — `adservice` | Java agent OTel `2.24.0`, exportação OTLP de logs | Log4j2 JSON stdout, sem IDs de trace/span |

Não foram adicionados logs para `productcatalogservice`: não houve chamada natural de logging no fluxo normal validado. O serviço continuou produzindo sinais de métrica/trace. `NOT_OBSERVED_DURING_NORMAL_WORKLOAD` para logs; não é um log ausente de erro causal observado neste fluxo.

### Ajustes de inicialização comprovados

- O agent Java era ilegível para UID 1000; o Dockerfile agora instala o JAR como `0444`.
- O agent precisa criar helpers temporários, mas o container mantém `readOnlyRootFilesystem`; o patch monta `/tmp` como `emptyDir` e define `-Djava.io.tmpdir=/tmp`.
- A inicialização instrumentada do Java leva mais que o probe inicial. Um `startupProbe` gRPC protege a inicialização; depois dele, readiness e liveness existentes continuam aplicáveis.
- O layout Log4j2 anterior escrevia os valores literais `${ctx:traceId}`, `${ctx:spanId}` e `${ctx:traceSampled}` no stdout. Eles não eram IDs, mas expunham campos sem correlação. Os campos foram removidos do layout na origem. Na janela final, stdout A de `adservice` não possuía nenhum campo trace/span.
- `paymentservice` passou a registrar o erro real de `Charge` com `logger.warn({err}, 'Payment charge failed')`, substituindo o `console.warn(err)` que não atravessava o logger Pino. A mensagem continua sendo o mesmo fato de erro; nenhum evento experimental foi criado.

## Pipelines de logs e duplicação

```text
A: application logger → stdout → Promtail A → Loki A
B: application logger → OTel Logs/OTLP → Collector B → Loki B
```

Collector B `0.123.0` possui receiver OTLP gRPC/HTTP, processor `batch` e pipeline `logs` para o endpoint OTLP de Loki B (`/otlp`). A rota foi exercitada por registros reais de todas as linguagens observadas.

O spike confirmou que coletar também o stdout pelo Promtail B duplicava um evento. A configuração vazia de Promtail deixava o DaemonSet não pronto. A solução mínima foi remover Promtail B, seu ConfigMap e RBAC: B não coleta stdout de aplicação nem logs de infraestrutura nesta etapa. Loki B permanece como backend para OTLP Logs. No intervalo final não houve stream B com label `filename` e não houve duplicação do evento de pagamento. Logs runtime/framework de B ficam fora da evidência desta etapa.

## Validação normal e evidência

Janela do fluxo final: `2026-11-16T17:48:04.272044Z`–`2026-11-16T17:48:08.218011Z` UTC. Foi executado um único fluxo host-side: `GET /`, `GET /product/OLJCESPC7Z`, `POST /cart`, `GET /cart`, `POST /cart/checkout`. Os cinco requests retornaram HTTP 200. Não houve fault nem Chaos.

- SUT: `11/11 Ready`; Loki A/B, Jaeger A/B, Prometheus A/B e Collector B `Ready`.
- Prometheus A: 11 séries `up` e 43 séries do conceito convencional de duração na consulta pós-fluxo.
- Prometheus B: 13 séries de duração RPC OTel observadas; Collector B exportou traces e logs.
- Jaeger A e B retornaram serviços do fluxo normal, incluindo os serviços de aplicação.
- Loki A recebeu application logs por stdout/Promtail; registros carregavam `filename` e `stream=stdout`.
- Loki B recebeu registros OTLP com `service_name`, severidade e, quando havia contexto ativo, `trace_id`/`span_id`; os registros não carregavam `filename`.
- Varredura dos registros A da janela não encontrou campos trace/span explícitos (`0` ocorrências).

### Amostra semântica representativa: `paymentservice/Charge`

A chamada natural do Pino `Transaction processed: visa ending 1111 Amount: USD28.980000000` ocorreu uma vez no fluxo:

- A: uma ocorrência em Loki A, severity `info`, stdout convencional, sem campos de correlação.
- B: uma ocorrência em Loki B, body equivalente e severity `info`, via OTLP.
- B: `trace_id=37a833770d89b8636cf2d011f9a7ec70`, `span_id=0cbc290087a2a95c`.
- Jaeger B contém exatamente o span `0cbc290087a2a95c`, operação `grpc.hipstershop.PaymentService/Charge`, serviço `paymentservice`, dentro do trace informado.

A mesma verificação de associação LogRecord→span no Jaeger passou para os logs naturais observados de `frontend`, `checkoutservice`, `cartservice`, `currencyservice`, `paymentservice`, `shippingservice`, `emailservice`, `recommendationservice` e `adservice` (9/9 serviços com logs correlacionados na janela). Nenhum ID foi sintetizado. `productcatalogservice` não emitiu log natural nesse fluxo.

A e B tiveram contagens RAW diferentes para alguns serviços. Isso não foi usado como critério de igualdade: A contém registros de health/runtime e sua saída Console de `cartservice` ocupa múltiplas linhas; B preserva chamadas de aplicação como LogRecords estruturados, com template e atributos. As chamadas relevantes de aplicação observadas têm fatos semânticos correspondentes; nenhum conteúdo foi igualado post-hoc.

## Testes e verificações

- Build Docker das dez imagens: PASS.
- Go tests: `frontend`, `checkoutservice`, `productcatalogservice`, `shippingservice` PASS (`-vet=off`; warning/vet preexistente de checkout não foi atribuído à integração).
- `paymentservice` npm test: comando PASS, suíte reporta `0` testes definidos; runtime normal forneceu a prova de integração.
- `currencyservice` sintaxe Node: PASS; seu script de teste upstream não define testes.
- .NET `cartservice` build Docker: PASS.
- Python `py_compile` para os dois serviços: PASS.
- Java Gradle `test`: BUILD SUCCESSFUL, `NO-SOURCE`; Docker build/`installDist`: PASS.
- Kustomize de B e configuração de Collector: PASS; a rota OTLP Logs/Loki foi confirmada por runtime.
- `git diff --check`: PASS.

## Isolamento e critérios da etapa

| Critério | Resultado |
|---|---|
| Scenario A mantém stdout convencional sem trace/span IDs | PASS |
| Scenario B mantém traces e métricas | PASS |
| Logs de aplicação chegam a Loki A e Loki B | PASS |
| OTLP Logs B preserva correlação real quando há contexto | PASS |
| Mesma chamada natural alimenta A e B | PASS |
| Backend A/B isolado por origem/pipeline | PASS |
| Duplicação sistemática de application logs em B | NONE |
| Shared Observation runtime abstraction | NONE |
| Evento/log artificial ou segunda chamada manual | NONE |
| Diferença crítica de cobertura sem explicação | NONE identificada nesta validação normal |

## Limitações conhecidas

- A cobertura normal não produz log em `productcatalogservice`; não foi fabricado um evento para o teste. Cobertura de logs causais em smokes F4/F5 ainda deve ser validada na etapa RAW semantic coverage.
- Loki B preserva metadata nativa de SDK/processo como structured metadata. Esta etapa não a remove; seleção de conteúdo model-visible pertence às etapas posteriores.
- Promtail B foi removido; logs runtime/framework não são recolhidos em B e não são evidência model-visible.
- `npm test` do `paymentservice` e `test` do Java não contêm testes unitários; a prova principal aqui é build e runtime real.

**Gate da Etapa 7: PASS.** Esta conclusão cobre somente instrumentação e validação normal de logs/metrics/traces. Não valida faults, coleta RAW, Dataset Builder, RCA Input nem qualquer bateria experimental.

## Erratum — cobertura de rota HTTP do `frontend` B

Durante a validação do Dataset Builder, foi encontrada uma assimetria não detectada pelo gate semântico original: as métricas HTTP do `frontend` A preservavam operações como `GET /` e `GET /cart`, enquanto o `otelhttp.NewHandler(handler, "frontend")` do Scenario B exportava método/status/endereço sem `http.route`. O Builder não pode recuperar a rota a partir dessas séries.

A correção substituiu esse wrapper genérico pela instrumentação idiomática `otelmux.Middleware("frontend")` aplicada ao router Gorilla Mux. A integração registra a rota resolvida nas métricas e spans OTel. Não foi adicionada métrica manual nem inferência no Builder.

Validação pre-commit em `2026-11-16`, sem fault: a imagem `tcc-experiment-v2/frontend:stage10-route-fix-validation` foi construída do snapshot local e carregada no Kind. Depois de duas requisições benignas, Prometheus B mostrou `http_route=/` e `http_route=/cart`, ambas com seus métodos/status. Jaeger B registrou `GET /`, `span.kind=server` e `http.route=/`. O mesmo fluxo retornou HTTP 200. O Deployment foi restaurado à imagem anterior ao teste; somente o Pod frontend de validação foi substituído. O Collector e os demais Deployments não foram alterados.

A consulta observou também `GET /_healthz` em B, enquanto o middleware convencional A exclui esse endpoint. O Dataset o classifica como health check RAW-only; nenhum conteúdo causal é removido.

Esta validação corrige a implementação e a cobertura do Scenario B, mas não substitui uma nova coleta RAW nem as novas validações semânticas F4/F5 após mudança de instrumentação. A coleta histórica anterior à correção permanece válida para os sinais que já registrava, mas não prova cobertura de `http.route` no frontend B.
