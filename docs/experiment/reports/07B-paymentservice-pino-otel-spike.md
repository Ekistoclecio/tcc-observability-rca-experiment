# Etapa 7B — Spike Pino + OTLP Logs no `paymentservice`

## Resultado

**`SPIKE_CORE_PASS_DUPLICATION_POLICY_REQUIRED`**. Uma chamada natural existente do Pino alimentou stdout convencional e, separadamente, um LogRecord OTLP com correlação real. O teste foi apenas uma requisição normal de `PaymentService/Charge`; nenhuma fault ou Chaos foi executado.

O Loki B recebeu duas cópias do mesmo registro: a cópia OTLP contextualizada e a cópia convencional relida por Promtail B. Portanto, o caminho central funciona, mas a política de duplicação precisa ser aplicada antes da implementação definitiva.

## Estado e proveniência do spike

- Branch: `experiment-v2`; commit base: `fdbd151c4e47fc561cd87150ac9e666ef27a5f33`.
- A árvore já continha alterações legítimas não commitadas da Etapa 7. Elas foram preservadas.
- Build classificado `SPIKE / NON-OFFICIAL`, sem commit: fingerprint SHA-256 da fonte/configuração `ebc7b96b23b1053abb673d674a3860d5152480e59b5dabdadeae3bd0282769da`; label OCI `SPIKE_NON_OFFICIAL_ebc7b96b23b1053a`.
- Imagem: `paymentservice:spike-7b-ebc7b96b`; digest do manifest list `sha256:5bbb9e1408605692a0ecf900238a29c3d7d71745af8b855ae7256edeb1f6d89f`; ID/config local `sha256:c014c0289bc9aa0a731fccd17d4ac83948523c986f56a97c6d3d15dad73765dc`. O Pod durante o spike expôs exatamente esse último valor como `imageID`. Manifest digest e image/config ID são categorias diferentes.
- Runtime observado: Node.js `24.22.1`; Pino `10.3.1`; `@opentelemetry/instrumentation-pino` `0.68.0`; OTel Node SDK e OTLP Logs exporter `0.222.0`; OTel API `1.9.1`.

## Chamada natural selecionada

`src/paymentservice/charge.js` já registra, com `logger.info`, `Transaction processed: ...` durante o fluxo normal de `Charge`. O call site não foi alterado e não foi criada segunda chamada para OTel. A instrumentação Pino foi registrada antes da criação do singleton do logger. O teste enviou uma única requisição válida para `PaymentService/Charge`, com valor sintético `USD75.432100000`.

## Evidência — Scenario A / stdout

A chamada produziu em stdout e no Loki A uma entrada convencional: `severity=info`, `name=paymentservice-charge`, mensagem `Transaction processed: visa ending 1111     Amount: USD75.432100000`. O registro stdout não contém `trace_id`, `span_id`, `traceId` ou `spanId`. Loki A retornou uma ocorrência para a mensagem selecionada.

## Evidência — Scenario B / OTLP Logs

O mesmo corpo chegou ao Loki B via OTLP com `service_name=paymentservice`, severidade `info` (número OTel 9), `trace_id=9fdc48f456db551bf5410e326d1936c4` e `span_id=a14add2444e6efbb`. O Jaeger B contém o trace `9fdc48f456db551bf5410e326d1936c4` e o span `a14add2444e6efbb`, operação `grpc.hipstershop.PaymentService/Charge`, kind `server`, gRPC status 0. Assim, os IDs do LogRecord correspondem ao span real ativo, sem reconstrução por Collector.

Rota usada: receiver OTLP existente do Collector B → pipeline `logs` com `batch` → exporter `otlphttp/loki-b` para `http://loki-b.observability-b.svc.cluster.local:3100/otlp`. O Loki preservou IDs como metadata estruturada e o corpo como linha.

## Duplicação

**`DUPLICATION_CONFIRMED`**. Na mesma janela e para o mesmo corpo, Loki B retornou duas entradas: (1) OTLP com `trace_id`/`span_id` e atributos OTel; (2) stdout com labels `filename` e `stream=stdout`, ingerido pelo Promtail B. Loki A retornou apenas a entrada stdout convencional.

Política mínima recomendada para a implementação definitiva: OTLP é a fonte de application logs do B; excluir os arquivos stdout de aplicação do Promtail B. Logs de runtime/infraestrutura, se mantidos, devem ter origem/categoria separada e permanecer audit-only, sem duplicar eventos de aplicação. Esta política não foi implementada neste spike.

## Diagnóstico das falhas durante o spike

1. Na primeira imagem, `logger` era instanciado antes do registro da instrumentação Pino; a instrumentação não interceptava o logger já criado. A primeira divergência estava na inicialização da aplicação. O logger passou a ser carregado depois do registro OTel.
2. Em seguida, a API efetiva exigiu `BatchLogRecordProcessor({ exporter })`; a construção inicial não exportava logs. Corrigida dentro do spike, sem alterar o call site.
3. A primeira atualização do ConfigMap continha o envelope Kubernetes em vez do YAML interno do Collector. O Collector recusou a configuração; o conteúdo foi corrigido e o rollout ficou Ready antes da requisição final.

Essas tentativas foram temporárias; não fazem parte do código retido.

## Avaliação científica

- Uma chamada Pino natural alimentou os dois caminhos: **PASS**.
- A manteve stdout convencional sem IDs explícitos: **PASS**.
- B recebeu IDs reais do span correspondente: **PASS**.
- Mesmo evento lógico e semântica de severidade INFO: **PASS**.
- Abstração runtime compartilhada, evento artificial ou segunda chamada manual: **NÃO**.
- Duplicação: **confirmada e explicada pela coleta simultânea de stdout via Promtail B e OTLP**.
- A arquitetura de dual sink é tecnicamente viável para `paymentservice`; o problema de duplicação exige política no pipeline B antes da validação definitiva.
- Este spike não demonstra compatibilidade dos demais serviços, nem aprova a Etapa 7 completa. Nenhuma mudança metodológica é necessária para o resultado observado.

## Restauração e validações

Após a captura, o Deployment voltou à imagem pré-spike `tcc-experiment-v2/paymentservice:f59da69ca86421347d801fd9ce865d88a9349ff9`, com Pod Ready e `imageID=sha256:a9f1562fd5a1693b4adae3bf04b94a13dd585735c41e2f35ce19e21d1c9eee87`. A ConfigMap e o Deployment do Collector B foram restaurados; Collector Ready `1/1`. Port-forwards temporários e as três tags de imagem do spike foram removidos.

Os arquivos de código/configuração exclusivos do spike foram restaurados às suas versões imediatamente anteriores ao spike, que já incluíam as alterações Stage 7 preexistentes. `node --check` passou; testes de `paymentservice`: 3 passaram, 0 falharam; `git diff --check`: PASS. A working tree continua dirty pelas alterações pré-existentes da Etapa 7 e pelos relatórios pedidos; nada foi descartado.

Sem commit permanente. Sem fault, sem LLM e sem início da Etapa 8.
