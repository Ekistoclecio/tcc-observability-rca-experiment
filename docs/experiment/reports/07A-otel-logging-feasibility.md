# Etapa 7A — Viabilidade de logs OpenTelemetry no Scenario B

## Resultado executivo

**Classificação: `CLEAR_PREFERRED_ARCHITECTURE` — preferência conceitual, ainda sem validação runtime.** A alternativa tecnicamente mais consistente com o desenho atual é encaminhar a mesma chamada do logger de aplicação por dois destinos: o stdout convencional permanece intacto para A e um appender/hook/provider idiomático envia o mesmo registro, sem nova chamada de log, como OTLP Logs para B. O Collector B encaminha esses registros a Loki B. A coleta de stdout de aplicação pelo Promtail B deve ser desativada para não duplicar os registros recebidos via OTLP.

Isso não significa que a implementação esteja pronta. A viabilidade e o modo de transportar o contexto precisam ser confirmados por linguagem, em especial para Pino/Node.js e para as chamadas Go que hoje não ligam explicitamente o `context.Context` ao `logrus.Entry`. A maturidade oficial atual de Logs varia: Java e .NET Stable; Go Release candidate; JavaScript e Python Development. [Status das APIs/SDKs OpenTelemetry](https://opentelemetry.io/docs/languages/)

Esta investigação não alterou os arquivos de implementação, não implantou as mudanças locais da Etapa 7 e não executou workload ou fault. O blocker continua sendo o registro runtime anterior: A e B ingerem o mesmo stdout convencional, e B não tem pipeline OTLP de logs.

## Estado investigado

- Branch: `experiment-v2`
- HEAD: `fdbd151c4e47fc561cd87150ac9e666ef27a5f33`
- Working tree: já estava suja antes desta investigação, com as alterações locais da Etapa 7 e os artefatos do blocker; elas foram preservadas. Os dois arquivos `07A` são os únicos arquivos criados por esta investigação.
- O relatório `BLOCKED-07-scenario-b.md` confirma que `collector.yaml` tinha pipelines somente de `traces` e `metrics`, e que `promtail-a` e `promtail-b` liam o mesmo `/var/log/pods/.../*.log` para destinos Loki separados.
- A configuração `kustomize/experiment-v2/observability-b/collector.yaml` da árvore local deve ser revisitada ao implementar, pois é a configuração ainda sem pipeline `logs`; não foi alterada aqui.

## Inventário por serviço

Versões abaixo vêm dos Dockerfiles e manifests de dependências presentes na working tree desta branch no momento da análise; OTel indica os pacotes locais de traces/métricas, não uma integração de Logs já instalada. A coluna “contexto natural” indica se a operação tem contexto disponível e se a chamada atual o entrega ao logger; disponibilidade de contexto não prova correlação automática.

| Serviço | Linguagem/runtime | Logger e destino atuais | OTel atual local (traces/métricas) | Logs naturais, contexto e sinks |
|---|---|---|---|---|
| `frontend` | Go 1.27.0 | Logrus 1.10.2, JSON em stdout | OTel Go 1.46.0; `otelhttp` 0.71.0, `otelgrpc` 0.71.0; OTLP traces e métricas | Logs de request, erro e checkout já existem. Middleware põe logger no request context; handlers leem esse logger, mas o hook OTel ainda precisaria receber contexto de trace no `Entry`. Logrus suporta hooks e output stdout simultâneos. |
| `checkoutservice` | Go 1.27.0 | Logrus 1.10.2, JSON em stdout | OTel Go 1.46.0; `otelgrpc` 0.71.0; OTLP traces e métricas | `PlaceOrder` e falhas de downstream já são registrados durante RPC. Métodos recebem `context.Context`, mas as chamadas atuais usam logger global, sem `Entry.Context` explícito. Hook é viável; associação do contexto requer logger contextual no caminho da chamada. |
| `cartservice` | C#/.NET 10.0; SDK/runtime 10.0.100/10.0.0 | `Console.WriteLine` em stdout nos caminhos de cart/datastore; `appsettings.json` configura logging Microsoft, mas os pontos de aplicação encontrados não injetam `ILogger` | OpenTelemetry .NET 1.21.0; instrumentação ASP.NET Core, `ActivitySource` custom para Redis, OTLP traces/métricas | Logs reais de Redis/cart existem dentro da operação. O runtime tem `Activity` ativo nos requests; `ILogger` correlaciona automaticamente via `Activity.Current`. `Console.WriteLine` não fornece provider/sink estruturado; conversão dos pontos existentes para `ILogger` seria necessária para dual sink idiomático, preservando o provider Console. |
| `productcatalogservice` | Go 1.27.0 | Logrus 1.10.2, JSON em stdout | OTel Go 1.46.0; `otelgrpc` 0.71.0; OTLP traces/métricas | Há logs naturais de operação e erro. Handlers têm contexto RPC, mas logs globais não o carregam automaticamente. Hook Logrus é viável; contexto precisa chegar ao `Entry`. |
| `currencyservice` | Node.js 24.20.0 | Pino 10.3.1, JSON em stdout | OTel JS API 1.9.1, SDK Node 0.222.0, gRPC instrumentation 0.222.0, OTLP trace exporter 0.26.0 e metric exporter 0.222.0 | Pino registra solicitações, resultados e erros de conversão. O plugin oficial `instrumentation-pino` documenta multistream para o stream atual e o OTel Logs SDK, e suporta Pino `<11`, incluindo a versão local 10.3.1. Porém correlação desabilitada no mixin e contexto no registro OTLP devem ser provados especificamente. |
| `paymentservice` | Node.js 24.20.0 | Pino 10.3.1, JSON em stdout; handler de `Charge` também chama `console.warn(err)` no caminho de exceção | Mesmas versões OTel JS de `currencyservice`; gRPC instrumentation e OTLP traces/métricas | Log natural `PaymentService#Charge` existe dentro do handler e da operação crítica F4. Pino dual sink é tecnicamente plausível. `console.warn` não passa pelo Pino e não seria enviado pelo bridge Pino; o tratamento do erro deve continuar sendo uma única chamada real de logging encaminhada, sem criar segunda mensagem. A integração precisa comprovar o contexto OTel no OTLP sem adicionar IDs ao stdout A. |
| `shippingservice` | Go 1.27.0 | Logrus 1.10.2, JSON em stdout | OTel Go 1.46.0; `otelgrpc` 0.71.0; OTLP traces/métricas | Logs de `GetQuote`/`ShipOrder` e erros existem. O handler tem contexto, mas chamadas globais não o vinculam automaticamente. Hook é viável com contexto ligado ao entry. |
| `emailservice` | Python 3.14.7 | `logging` padrão + `python-json-logger`, `StreamHandler(sys.stdout)`, `propagate=False` | OTel API/SDK/exporter 1.39.1; instrumentation gRPC 0.60b1; traces e métricas locais | Eventos reais de envio e erro existem dentro de handlers gRPC. Pode-se anexar `LoggingHandler` ao mesmo logger sem substituir o handler stdout; o OTel Python documenta este bridge, mas a implementação de Logs segue Development. Contexto ativo é suportado pelo modelo/context do SDK; provar correlação no runtime desta versão ainda é necessário. |
| `recommendationservice` | Python 3.14.7 | `logging` padrão + formatter JSON custom, stdout, `propagate=False` | OTel API/SDK/exporter 1.39.1; instrumentation gRPC 0.60b1; traces e métricas | Há logs do handler de recomendações e do cliente. Mesmo modelo que `emailservice`: handler OTel adicional possível, sem trocar a chamada, com risco de maturity Development e validação de contexto. |
| `adservice` | Java 25 (imagem); source compatibility 21 | Log4j2 Core 2.26.1 e `log4j2.xml`, JSON em stdout | Java agent OTel 2.24.0 (imagem/patch local), traces/métricas por agente | Logs naturais de RPC e falhas existem. A config Log4j já possui campos MDC de trace/span destinados ao formato legado, mas a população efetiva depende do agente/contexto; não presumir que chegam em A. A implementação Java tem Logs Stable; Log4j OTel appender/bridge é opção idiomática e deve alimentar o logger provider do agente sem duplicar a chamada. O ecossistema lista o artefato do appender Log4j2 como `alpha`; maturity Stable do SDK Java não torna esse bridge específico Stable. |

**C++:** não é linguagem de nenhum dos dez serviços SUT atuais; portanto não há serviço C++ a compatibilizar nesta tarefa.

### Fontes de logging por grupo

- Os quatro serviços Go (frontend, checkout, product catalog, shipping) usam Logrus e stdout JSON. O bridge `otellogrus` converte entradas Logrus em OTel LogRecords, incluindo campos; sua viabilidade depende de passar o contexto da operação ao `Entry` para obter correlação no hook. [Bridge oficial Logrus](https://pkg.go.dev/go.opentelemetry.io/contrib/bridges/otellogrus)
- Os dois serviços Node usam Pino. A instrumentação oficial Pino adiciona um destino multistream ao stream original e ao OTel Logs SDK; as duas chamadas de destino derivam da mesma chamada Pino. Porém o mecanismo de correlação documentado injeta trace fields no registro Pino compartilhado; a versão atual do código também remove esses campos antes de emitir pelo OTel stream, sem mostrar passagem explícita do contexto em `emit`. Por isso, o sink preservando o contexto enquanto o stdout permanece sem IDs é uma condição que precisa de prova runtime/integração, não uma conclusão desta auditoria. [Pino OTel instrumentation README](https://github.com/open-telemetry/opentelemetry-js-contrib/blob/main/packages/instrumentation-pino/README.md), [instrumentação Pino — implementação](https://github.com/open-telemetry/opentelemetry-js-contrib/blob/main/packages/instrumentation-pino/src/instrumentation.ts), [OTel Pino stream — implementação](https://github.com/open-telemetry/opentelemetry-js-contrib/blob/main/packages/instrumentation-pino/src/log-sending-utils.ts)
- Os dois serviços Python usam `logging` padrão e um handler stdout próprio. `LoggingHandler` do SDK é um bridge ao logger existente; com handler adicional e exportador OTLP, a mesma chamada pode alimentar stdout e OTel. A documentação mostra explicitamente o uso do logger padrão com `LoggerProvider`, `LoggingHandler` e processor. [OpenTelemetry Python logging](https://opentelemetry.io/docs/languages/python/instrumentation/)
- `cartservice` pode usar o pipeline Microsoft `ILogger`, mantendo `Console` e acrescentando provider OTel. .NET documenta coexistência de providers e associação automática de TraceId/SpanId à `Activity` ativa. O obstáculo atual é a aplicação usar `Console.WriteLine` nos pontos reais, não falta de suporte do runtime. [OpenTelemetry .NET logs](https://opentelemetry.io/docs/languages/dotnet/logs/), [correlação .NET](https://opentelemetry.io/docs/languages/dotnet/logs/correlation/)
- `adservice` usa Log4j2; há integração/appender Log4j listado pelo ecossistema oficial Java, mas o artefato mostrado na matriz está em `alpha`. A opção deve compartilhar o logger provider usado pelo agente OTel e manter o appender de console existente. Versão exata compatível com o agente 2.24.0 e com configuração atual precisa ser fechada ao implementar. [Ecossistema de instrumentação Java](https://opentelemetry.io/docs/languages/java/instrumentation/)

## Maturidade e compatibilidade

O estado oficial de maturidade é do componente principal OTel de cada linguagem e pode não representar a maturidade de cada bridge. A especificação Logs é Stable, mas os SDKs são heterogêneos. [Maturidade dos sinais](https://opentelemetry.io/docs/languages/), [OpenTelemetry Logs](https://opentelemetry.io/docs/concepts/signals/logs/)

| Linguagem | Estado oficial de Logs | API/SDK/exporter utilizável | Correlação | Compatibilidade observada com runtime local | Impacto/riscos |
|---|---|---|---|---|---|
| Go | `RELEASE_CANDIDATE` | bridge `otellogrus`, SDK e OTLP disponíveis no ecossistema; versão exata do bridge deve ser alinhada a OTel 1.46 | Não presumir automática no Logrus: o bridge usa contexto associado ao entry; chamadas atuais muitas vezes não o fornecem | Go 1.27.0 e OTel 1.46 já existem na working tree; nenhum upgrade de runtime identificado | Dependência em sinal/bridge RC; context plumbing em caminhos de request. |
| JavaScript/Node | `DEVELOPMENT` | OTel JS Logs API/SDK e OTLP exporter existem; `instrumentation-pino` suporta Pino 10, mas requer dependência e validação da correlação OTLP | Pino pode adicionar trace fields ao registro; o plugin os compartilha com stdout e a implementação do OTel stream depende de verificar contexto/context manager | Node 24.20.0 é compatível com stack local; Pino 10.3.1 está no intervalo suportado pelo plugin | Risco técnico mais alto: separar stdout A do contexto OTel B sem outro evento nem IDs no stdout. |
| Python | `DEVELOPMENT` | `LoggerProvider`, `LoggingHandler`, OTLP exporter são documentados; versões locais 1.39.1/0.60b1 | Via contexto OTel ativo do SDK; verificar comportamento concreto com gRPC instrumentation local | Python 3.14.7 e dependências atuais; nenhum upgrade indicado pela documentação examinada | SDK de Logs em Development; risco de estabilidade/semconv, sem necessidade aparente de mudar runtime. |
| .NET | `STABLE` | `ILogger` + OTel provider/exporter; atual OpenTelemetry 1.21.0 | Automática por `Activity.Current` no SDK OTel .NET | .NET 10 local; sem upgrade indicado | Requer trocar `Console.WriteLine` nos pontos de cart por `ILogger` preservando Console provider. |
| Java | `STABLE` | Java agent e Log4j appender/bridge OTel disponíveis | Agente/integração de logging pode correlacionar via contexto e MDC | Java 25, compatibilidade source 21, agente 2.24.0; sem upgrade indicado | Fechar compatibilidade do appender com agent version; config Log4j atual inclui chaves MDC de trace, então evitar vazamento/duplicação. |

JavaScript, Python e Go estão em maturidade menor que .NET/Java (Development ou Release candidate); isso é risco de manutenção para uma implementação comercial, não prova de inviabilidade. O projeto já usa esses runtimes e APIs para traces/métricas; não foi encontrada necessidade documentada de upgrade de Node, Python, Go, .NET ou Java para adicionar o caminho de logs. **Não foi feita compilação nem teste de integração de qualquer opção de Logs nesta análise.**

## Serviços críticos

### `checkoutservice` — Go/Logrus

O fato natural de operação já é registrado no caminho de `PlaceOrder`, incluindo início, resultado do pagamento e falha de downstream. Os handlers recebem `context.Context`; o hook `otellogrus` fornece rota idiomática sem duplicar a chamada. Para correlação real, a chamada precisa ocorrer via entry que carregue o contexto. É viável sem nova abstração compartilhada, com alteração delimitada no logger contextual ou nos pontos de logging relevantes. O estado Release candidate do OTel Go permanece risco.

### `paymentservice` — Node/Pino

`PaymentService.Charge` já emite log natural dentro do handler e da operação que terá trace ativo; Pino 10.3.1 é suportado pela instrumentação oficial de Pino, que documenta fan-out da mesma chamada para stdout e OTel Logs. Isso torna a integração plausível sem novo `logger.error` específico para B. O ponto não provado é o requisito experimental simultâneo: o stdout consumido por A deve continuar sem correlação explícita, mas o LogRecord OTLP de B precisa conter TraceId/SpanId. O plugin padrão documenta injeção de trace fields no registro Pino e opção para desativar essa injeção; o caminho de envio OTel remove os campos serializados antes do `emit`, e a implementação consultada não mostra passagem explícita de contexto. Exige pequeno spike/normal-runtime validation antes de aprovar essa implementação concreta. Além disso, `console.warn(err)` no catch de Charge não é Pino e não será coberto pelo bridge Pino; o fato de erro deve usar o logger da aplicação uma vez, não uma chamada adicional.

### `cartservice` — .NET/Console

Logs reais de datastore/cart estão em `Console.WriteLine`; embora exista `ILogger` registrado pela infraestrutura padrão, os pontos observados não o usam. O caminho idiomático seria `ILogger` com os providers Console e OpenTelemetry, correlacionado à `Activity` atual. É possível representar a mesma ocorrência em stdout A e OTLP B sem duplicar chamada de log, mas migrar esses call sites é mudança de aplicação e precisa manter formatação/logging convencional A. O SDK .NET estável torna esta rota de menor risco técnico que Node/Python/Go.

## Arquiteturas candidatas

### A — dual sink idiomático, stdout convencional mais OTel Logs separado

```text
uma chamada no logger existente
       ├── stdout convencional → Promtail A → Loki A
       └── OTel Logs/OTLP → Collector B → Loki B
```

É comercialmente plausível: frameworks de logging já suportam appenders/hooks/providers, e a especificação OTel recomenda bridge/appender existente como abordagem de integração. Preserva o registro natural e o tratamento A; evita Shared Observation runtime abstraction, log artificial e parsing downstream para inventar contexto. Complexidade é moderada e distribuída por 5 stacks de logging. Não exige nova chamada por sinal/serviço; exige configuração e algumas ligações de contexto/call sites, e migração de `Console.WriteLine` no cart.

### B — stdout contextualizado e filtro posterior para A

**UNSAFE.** Se B injeta `trace_id`/`span_id` no registro compartilhado, o mesmo stdout também é visível ao caminho A antes do filtro; A já está contaminado na origem/RAW. Retirar os campos no Promtail A não recupera a telemetria convencional original: cria tratamento post-hoc assimétrico e perde a integridade do RAW. Não atende a preservação de A.

### C — Collector/filelog enrichment do stdout simples

**NO.** Resposta a “um Collector recupera o `trace_id`/`span_id` verdadeiro se nunca foram emitidos no log?”: **NO**. Filelog/parser pode extrair somente campos presentes; processor pode enriquecer com metadados conhecidos, mas não reconstituir qual span estava ativo no momento do evento. Timestamp, serviço e operação permitem correlação heurística/temporal, não correlação real. Logs OTel especificam TraceId/SpanId como contexto do LogRecord e recomendam appender/bridge no produtor. [Modelo de Logs OTel](https://opentelemetry.io/docs/specs/otel/logs/)

### D — remover requisito de correlação de logs em B

Tecnologicamente simples, porém exige alteração explícita do desenho: a hipótese e o tratamento perderiam o componente de correlação log↔trace, ficando concentrados em estrutura/contexto dos demais sinais. Pode continuar respondendo uma questão de qualidade de telemetria mais estreita, mas reduz validade de construto para a questão atual; não fazer silenciosamente nem editar `experiment-design.md` nesta tarefa.

### E — correlacionar apenas linguagens com suporte maduro

**POOR** para o desenho atual. .NET/Java receberiam um tratamento diferente de Go/Node/Python. Os faults e as operações críticas distribuem-se por essas linguagens; a qualidade de diagnóstico passaria a covariar com serviço/linguagem e tipo/localização de fault. Não é justificável como tratamento uniforme sem redesenho/estratificação do experimento. Não recomendar.

## Matriz comparativa

| Critério | A: dual sink idiomático | B: stdout contextual + filtro | C: Collector enrichment | D: remover correlação B | E: suporte parcial por linguagem |
|---|---|---|---|---|---|
| Preserva A | **GOOD** — stdout permanece convencional | **UNSAFE** — IDs entram na fonte compartilhada; filtro não restaura RAW original | **GOOD** para stdout, mas não cria B correlation | **GOOD** | **GOOD** |
| Correlação B real | **GOOD**, condicionada a passar contexto pelo bridge/appender; Pino necessita prova | **GOOD** para B, mas vaza para A | **UNSAFE** — não há contexto a recuperar | **POOR** — requisito removido | **GOOD** apenas em subset |
| Validade ecológica | **GOOD** — extensão comum de frameworks de produção | **POOR** — filtragem pós-emissão é artificial | **POOR** se alegar correlação real; enrichment sem contexto é heurística | **ACCEPTABLE** somente após revisão científica | **POOR** — tratamento inconsistente por linguagem |
| Validade interna | **GOOD**, se chamadas e eventos semânticos equivalentes forem preservados | **UNSAFE** — contaminação do braço A | **UNSAFE** — correlação falsa/heurística | **POOR** para hipótese atual; exige mudança explícita | **POOR** — confunde tratamento e localização do fault |
| Complexidade | **ACCEPTABLE** — configuração multi-framework e Collector; moderada | **ACCEPTABLE** operacionalmente, mas metodologicamente inválida | **ACCEPTABLE** operacionalmente, mas não resolve a pergunta | **GOOD** tecnicamente; alto custo metodológico/documental | **POOR** pela matriz/explicação adicional |
| Viabilidade cross-language | **ACCEPTABLE** — mecanismos idiomáticos existem para todos, maturidade varia | **GOOD** tecnicamente | **UNSAFE** para correlação verdadeira | **GOOD** | **POOR** |
| Upgrade de runtime | **GOOD** — nenhum necessário encontrado; validar versões de bridge | **GOOD** | **GOOD** | **GOOD** | **GOOD** |
| Risco de duplicação | **ACCEPTABLE** — controlável removendo stdout de aplicação do Promtail B | **GOOD** se um único stdout | **GOOD** | **GOOD** | **ACCEPTABLE** |
| Mudança de design necessária | **GOOD** — não | **UNSAFE** — incompatível sem filtro | **UNSAFE** — não satisfaz tratamento | **POOR** — sim | **POOR** — provavelmente sim |

## Collector B → Loki B e risco de duplicação

A rota técnica mínima é reutilizar o receiver OTLP já existente no Collector B, adicionar `logs` pipeline com `batch`, e exporter `otlphttp` para Loki B usando endpoint base `http://loki-b:3100/otlp`. A documentação Loki indica que o exporter compõe `/otlp/v1/logs`; configurar a URL completa causa endpoint incorreto. O Loki atual do projeto é 3.5.5, portanto suporta ingestão OTLP e structured metadata. [Tutorial oficial Collector→Loki](https://grafana.com/docs/loki/latest/send-data/otel/otel-collector-getting-started/), [API OTLP do Loki](https://grafana.com/docs/loki/latest/reference/loki-http-api/)

Se Promtail B continuar coletando o stdout de aplicação e Loki B também receber o mesmo registro via OTLP, **sim, há risco de duas cópias lógicas** do mesmo log. O desenho mais limpo para logs de aplicação é tornar OTLP a única fonte de application logs em B e deixar Promtail A como fonte convencional A. Em B, stdout/stderr de runtime e infraestrutura pode permanecer em stream/audit store separado, com origem explicitamente identificada e fora do conjunto RCA; não misturar com os logs de aplicação OTLP. Alternativamente Promtail B pode continuar somente para categorias que não são exportadas por OTLP, desde que a separação seja por fonte/categoria e não por mensagem/fault. O destino exato dos logs não-app da B precisa ser decidido na implementação para que RAW continue auditável sem duplicar application events.

## Paridade semântica e tratamento

Em princípio, a mesma chamada natural — por exemplo o logger já usado no handler Charge — pode produzir um registro convencional A e um LogRecord OTel B com o mesmo fato, mantendo `event/message`, serviço, nível, operação e erro equivalentes. Os campos nativos e a representação podem divergir. Isso não exige `emit_shared_event(...)`.

A semântica não pode ser inferida só pela igualdade de texto: Pino pode converter severity e atributos; Logrus hook converte fields; .NET transforma `Console.WriteLine` em `ILogger`; Java appender traduz Log4j event. A futura validação normal precisa comparar fatos reais no stdout A e backend B e provar TraceId/SpanId no LogRecord B, sem esses IDs na fonte A.

## Recomendação

**Preferir a arquitetura A: bridge/appender/provider idiomático e dual sink por chamada de logging existente.** Ela é a única candidata que simultaneamente preserva a emissão convencional de A, dá a B uma rota independente para correlação OTel real e se mantém próxima da adoção comercial normal de OTel Logs. Manter cada chamada natural como fonte factual única; não criar um contrato runtime comum.

Condições antes de implementar ou encerrar a Etapa 7:

1. fazer uma prova pequena, sem fault, de um log Pino com span ativo para demonstrar que o OTLP LogRecord possui contexto top-level enquanto stdout convencional permanece sem IDs; se plugin padrão não cumprir isso, avaliar adapter Pino idiomático de dual sink que capture contexto no momento da chamada, sem serializar IDs no stream A;
2. confirmar que os handlers Logrus recebem contexto na entry para correlação; bridge por si só não prova contexto;
3. no cart, preservar o mesmo logger message ao substituir `Console.WriteLine` por `ILogger` com Console + OTel providers;
4. validar appender Log4j com a versão de Java agent local e evitar duplicação/mistura com MDC no stdout;
5. remover application stdout do scrape Promtail B se os mesmos eventos entrarem via OTLP; escolher separadamente se runtime/infrastructure logs B ficam em audit-only;
6. acrescentar pipeline Collector `logs` e exporter Loki OTLP; então validar uma execução normal real, sem faults.

**Alternativa:** se a correlação OTel independente não puder ser comprovada no Pino sem contaminar A ou criar uma chamada paralela, a saída metodologicamente honesta é interromper e submeter a revisão formal do desenho (opção D), não filtrar stdout, fabricar correlação ou aceitar suporte parcial por serviço.

## Respostas de controle

- Arquitetura atual: stdout convencional compartilhado entre Promtail A/B; Collector B recebe traces e métricas, sem logs.
- Suporte OTel Logs por serviço: todos têm caminho plausível, mas maturity desigual; nenhum caminho foi testado aqui.
- Serviços críticos: checkout e cart são viáveis via Logrus context hook e .NET `ILogger`; payment é viável em princípio via Pino, com risco específico de separação contexto/stdout a provar.
- Candidata recomendada: A (dual sink idiomático e OTLP B separado).
- Duplicação: provável se Promtail B e OTLP enviarem o mesmo log de aplicação para Loki B.
- Collector → Loki: tecnicamente suportado; usar endpoint base `/otlp` e logs pipeline.
- `design changes required`: **NO**, para opção A; **YES**, para opção D ou E.
- `new physical fault required`: **NO** para esta análise e para o próximo passo de implementação de logs; a validação exigida é normal, sem fault.
- `new normal runtime validation required`: **YES**.
- Questões abertas: Pino top-level context mantendo stdout sem trace fields; compatibilidade de versões exatas dos bridges Logrus/Log4j; destino audit-only de stdout runtime/framework B; política de shutdown/flush do provider para não perder logs.

## Referências técnicas

- [Status oficial das APIs/SDKs por linguagem](https://opentelemetry.io/docs/languages/)
- [OpenTelemetry Logs: bridge para logging existente e correlação](https://opentelemetry.io/docs/concepts/signals/logs/)
- [Logs specification](https://opentelemetry.io/docs/specs/otel/logs/)
- [OpenTelemetry Go Logrus bridge](https://pkg.go.dev/go.opentelemetry.io/contrib/bridges/otellogrus)
- [OpenTelemetry Python logging bridge](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [OpenTelemetry .NET logs](https://opentelemetry.io/docs/languages/dotnet/logs/)
- [OpenTelemetry .NET automatic trace correlation](https://opentelemetry.io/docs/languages/dotnet/logs/correlation/)
- [OpenTelemetry Java logging ecosystem](https://opentelemetry.io/docs/languages/java/instrumentation/)
- [OpenTelemetry Pino instrumentation](https://github.com/open-telemetry/opentelemetry-js-contrib/blob/main/packages/instrumentation-pino/README.md)
- [Grafana Loki OTLP Collector pipeline](https://grafana.com/docs/loki/latest/send-data/otel/otel-collector-getting-started/)
- [Grafana Loki OTLP HTTP endpoint](https://grafana.com/docs/loki/latest/reference/loki-http-api/)

## Arquivos e componentes afetados em eventual implementação futura

Somente como referência diagnóstica: os loggers e dependências dos serviços listados acima; `kustomize/experiment-v2/observability-b/collector.yaml`; `kustomize/experiment-v2/observability-b/loki-promtail.yaml`; e, se a decisão for manter apenas OTLP application logs em B, a seleção de fontes de aplicação no Promtail B. Nenhum desses arquivos foi alterado nesta tarefa.
