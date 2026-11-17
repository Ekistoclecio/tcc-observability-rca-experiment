# F2 Diagnosis — Etapa 12A

## Estado e escopo

- Branch: `experiment-v2`
- Commit executado: `7c1c1f8f1a076a9a10cef0afcfc83417a7c91fb5`
- Execução nova: uma única `DIAGNOSTIC_F2_ONLY`; sem outras faults.
- Provenance: a evidência preservada da bateria registra 10/10 PASS. Read-only live check confirmou, para os mesmos dez Pods, image references e config image IDs registrados, OCI revision `7c1c1f8f1a076a9a10cef0afcfc83417a7c91fb5` em cada imagem local e Pods Ready.
- Ambiente: apenas `tcc-experiment-v2-stage1` estava em execução; `tcc-baseline` permaneceu parado/intocado. Backends Prometheus, Loki e Jaeger A/B retornaram health HTTP 200. Não havia Chaos antes da execução.
- SUT antes/depois: 11/11 Ready. A F2 foi removida; zero recursos Chaos residuais.

A configuração executada veio de `fault_smoke.py` e `config/experiment/faults.json`: NetworkChaos delay, source `app=checkoutservice`, target `app=paymentservice`, `direction: to`, 100 ms, jitter 0, correlation 100%, duração failsafe 2m. Nenhum arquivo de implementação foi alterado. A bateria oficial anterior continua interrompida e inválida para composição com uma campanha futura.

## Track N — efeito da falha de rede

### Destinos e caminho observado

| Item | Valor |
|---|---|
| Checkout `PAYMENT_SERVICE_ADDR` | `paymentservice:50051` |
| Service `paymentservice` ClusterIP | `10.96.106.113:50051` |
| Endpoint/Pod IP | `10.244.0.37:50051` |
| Peer do socket de checkoutservice durante/depois do fluxo | `10.96.106.113:50051` |
| Target ipset criado pelo Chaos Daemon | `10.244.0.37/32` |

Os três spans CLIENT B de pagamento na coleta normal anterior também registram `server.address=10.96.106.113`, `server.port=50051`, com durações de 3.573, 4.794 e 3.956 ms. O socket real do checkoutservice confirmou o peer ClusterIP. A regra de classificação fica no namespace de rede do checkoutservice, antes da tradução de Service feita fora desse namespace; portanto o destino observado no ponto de saída/classificação é o ClusterIP. Não foi usada captura de pacotes; a observação do peer do socket é complementada pelos contadores da regra e do qdisc.

### Contadores na F2 diagnóstica

- Antes da request, após `Selected=True` e `AllInjected=True`: `CHAOS-OUTPUT` 0 pacotes; regra `TC-TABLES-0` 0; `netem` 0.
- Depois de uma sequência curta com um checkout HTTP 200: `CHAOS-OUTPUT` 186 pacotes / 22,393 bytes.
- Regra `CLASSIFY` com `dst ∈ ipset(payment Pod IP)`: **0 pacotes / 0 bytes**.
- Qdisc `netem delay 100ms`: **0 pacotes / 0 bytes**.
- Qdisc de tráfego não classificado (`sfq` da banda padrão) recebeu tráfego; classe `1:4` que alimenta `netem` permaneceu em 0.

A combinação prova que o Chaos foi instalado, mas o tráfego da aplicação passou pelo hook e não casou com o matcher que seleciona o Pod IP. Assim, o atraso configurado não foi aplicado à conexão real para o Service ClusterIP. A sequência de checkout terminou com HTTP 200; o span B de `PaymentService/Charge` mediu 7.155 ms nesta execução, também sem o atraso configurado.

**Conclusão Track N:** `PROVEN` — `NETWORK_TARGET_MATCH_MISMATCH_PROVEN` / `FAULT_CONFIGURED_BUT_NOT_MATCHING_REAL_TRAFFIC`.

Um probe TCP adicional a ClusterIP e Pod IP foi tentado depois de a F2 já ter sido removida. Por estar fora da janela, seus resultados foram excluídos e não sustentam nenhuma conclusão.

## Track T — spans Scenario B

### Controle normal preservado

A coleta normal anterior contém três spans CLIENT B `checkoutservice → PaymentService/Charge`, durações 3.573, 4.794 e 3.956 ms. Isso confirma cobertura normal antes da execução interrompida.

### Execução F2 oficial interrompida

No RAW B de `checkoutservice` da janela F2 não há spans da transação: há somente três spans de `grpc.health.v1.Health/Check`. O RAW B de `paymentservice` contém uma span SERVER `PaymentService/Charge` (trace `657fd6b302def608cf19d9ca1e04a59c`, duração 1.226 ms). Consulta direta a esse trace ID no Jaeger encontrou o trace, ainda sem spans de `checkoutservice`/CLIENT de pagamento. Isso exclui uma simples omissão da consulta por serviço para esse trace específico, mas não distingue span não criado na origem de perda no caminho de exportação antes do Jaeger.

### F2 diagnóstica isolada

Para a request diagnóstica, Jaeger B encontrou o trace `cba836bffb4f27693c11106eeb8cc457` na primeira consulta, T+1s. Consultas ao mesmo trace em T+2, +5, +10, +20, +30 e +60s continuaram retornando-o. O trace contém checkoutserver `POST /cart/checkout`, `CheckoutService/PlaceOrder`, CLIENT `PaymentService/Charge` (7.155 ms) e SERVER `PaymentService/Charge` (3.860 ms). O CLIENT possui parent reference para `CheckoutService/PlaceOrder`; a span SERVER de pagamento veio sem parent reference, embora esteja agrupada no mesmo trace ID. Essa relação de parent permanece uma observação não resolvida, não uma explicação para a ausência histórica do CLIENT.

O Collector B, lido após a request, reportou totals cumulativos `accepted_spans=59,423`, `sent_spans=59,423`, `send_failed_spans=0`, `refused_spans=0`, `queue_size=0`; esses números são snapshot global, não delta exclusivo da janela. Os logs do checkoutservice durante a request não mostram erro de export. O trace apareceu no Jaeger dentro de 1s e não chegou tardiamente durante a janela de observação.

Sampling do checkoutservice: `AlwaysSample`. O código usa `sdktrace.WithBatcher(exporter)` sem opções BSP. O módulo fixado nos arquivos Go é OTel SDK v1.46.0; os defaults upstream correspondem a schedule delay 5s, queue 2048, export batch 512 e export timeout 30s ([fonte oficial v1.46.0](https://github.com/open-telemetry/opentelemetry-go/blob/v1.46.0/sdk/trace/batch_span_processor.go)). Não há indicação de sampler descartando somente o span CLIENT. O comando `go` não está instalado no host; a versão foi lida de `go.mod`/`go.sum`, sem executar build/testes.

**Conclusão Track T:** a nova execução mostra `B_TRACE_NORMAL` para presença/consulta do CLIENT e descarta atraso >1s nesta execução. A causa da ausência no RAW oficial anterior permanece `B_TRACE_CAUSE_NOT_PROVEN`; não há evidência histórica de export failure, Collector drop ou late arrival.

## Relação entre Tracks

`RELATION_NOT_PROVEN`. A configuração F2 que não casou o tráfego explica a falta de efeito de rede. Ela não explica, com a evidência disponível, por que os spans transacionais de checkoutservice faltaram no trace/RAW B da execução oficial. A execução diagnóstica seguinte produziu o CLIENT normalmente.

## Divergência, impacto e opções

- Observado: ipset contém Pod IP; aplicação conecta ao ClusterIP; 186 pacotes passam pelo hook, nenhum casa com o `CLASSIFY`, e `netem` permanece sem pacotes.
- Esperado: pacotes na aresta checkoutservice→paymentservice entram na banda `1:4` e recebem 100 ms.
- Primeira divergência: correspondência destino/IP no matcher `TC-TABLES-0` dentro do namespace do checkoutservice.
- Camada: `FAULT CONFIGURATION / NETWORKCHAOS TARGET MATCH`, não instrumentação nem validator.
- Invariante: ambos os cenários devem observar aumento de duração na aresta lógica F2; continua não satisfeito.
- Impacto científico: F2 anterior não aplicou o tratamento de latência à chamada real; a bateria interrompida permanece inválida/incompleta.

Opções para uma tarefa posterior, sem implementação nesta etapa:

1. Ajustar o alvo/matcher para o ClusterIP observado no ponto de saída, mantendo escopo apenas à aresta e os 100 ms. Esta é a correção candidata mais direta, mas ainda requer verificar suporte e efeitos colaterais da resolução/seleção de Service no mecanismo atual.
2. Aplicar a latência em um ponto após a tradução do ClusterIP para endpoint, restringindo simultaneamente a origem checkoutservice e o destino paymentservice. Isso preserva a semântica, mas pode exigir um mecanismo de fault diferente e análise de topologia.

Recomendação: decidir primeiro se o NetworkChaos atual consegue casar o ClusterIP de forma seletiva e estável. Não trocar o target ou o modo nesta etapa. Qualquer solução escolhida requer nova validação física F2 e, antes da bateria oficial futura, reinício da bateria em F1.

## Cleanup e limites

- NetworkChaos removido e recovery confirmado.
- Depois da limpeza: 11/11 Deployments SUT Ready e zero NetworkChaos/StressChaos.
- `tcc-baseline` permaneceu intocado.
- Não houve alteração de código/configuração, fault adicional, commit ou continuação da bateria.
- Working tree permanece com as alterações/deleções preexistentes registradas antes da tarefa; foram adicionados somente este relatório, seu JSON e a evidência resumida.
