# Arquitetura atual observada

## Desenho científico reconstruído

- **Variável independente:** representação, estrutura semântica, propagação de contexto e correlação de sinais na telemetria.
- **Cenário A:** métricas Prometheus convencionais, logs tradicionais/JSON via stdout, spans Zipkin/custom e propagação própria (x-tcc-trace-id, x-tcc-span-id).
- **Cenário B:** métricas, logs e traces OpenTelemetry, contexto W3C/OTel e correlação explícita de logs com trace/span quando existe contexto.
- **SUT:** Online Boutique, dez serviços de aplicação e redis-cart; loadgenerator é workload/control plane, fora da evidência RCA.
- **Workload:** scripts/experiment/run-workload.sh, cenário de carga aberto. A revisão recente de instrumentação não altera o workload.
- **Faults:** F1 CPU StressChaos em checkoutservice; F2 NetworkChaos checkoutservice→paymentservice; F3 paymentservice scale-to-zero; F4 flag local que faz Charge falhar; F5 redis-cart scale-to-zero.
- **Ground Truth:** produzido pelo orquestrador/mecanismo de injeção, com requested/confirmed/recovery timestamps e parâmetros. Não é derivado de telemetria. O Builder usa a janela/eligibilidade; requests model-visible não recebem GT.
- **RAW:** Prometheus A/B, Loki A/B e Jaeger A/B; global e por run, não sanitizados.
- **Dataset:** janela RCA e Common RCA View em scripts/experiment/dataset/.
- **RCA Input:** projeção model-visible, schema/prompt versionados em scripts/experiment/rca/. Não há chamada de modelo.
- **Avaliação científica:** não existe ainda no pipeline executado; nenhuma LLM/evaluator foi usada nesta auditoria.

## Arquitetura efetiva

~~~text
                         mesma aplicação/pod + mesma carga
                                      │
                       wrappers/interceptors por linguagem
                                      │
                    completion semântico em boundaries locais
                             ┌────────┴─────────┐
                             │                  │
                     adapter convencional   adapter OTel
                       métricas A             métricas B
                       stdout/logs A          OTLP logs B
                       Zipkin/custom A        OTel/auto spans B
                             │                  │
               Prometheus/Loki/Jaeger A   Collector/Prometheus/Loki/Jaeger B
                             └────────┬─────────┘
                                 Dataset Builder
                           janela → normalização/compactação
                                      │
                              RCA Dataset A/B
                                      │
                            projection + schema/prompt
                                      │
                              RCA Input A/B
~~~

Os “cenários” são duas rotas simultâneas geradas pelos mesmos pods, não dois clusters ou dois SUTs independentes. Isso é apropriado para parear a execução física. O tratamento de instrumentação é mantido por adapters e propagadores distintos.

## Isolamento de backend

- Prometheus, Loki e Jaeger A estão no namespace convencional; B usa Collector e backends no namespace OTel.
- Prometheus A coleta endpoints convencionais e cAdvisor; B coleta o endpoint Prometheus do Collector, que também recebe cAdvisor.
- Promtail A lê logs CRI de pods e envia ao Loki A; o Collector recebe logs OTLP e envia ao Loki B.
- Jaeger A recebe Zipkin/custom spans; B recebe OTLP traces.
- O rótulo de cenário é operacional/auditável nos backends e não deve ir ao RCA Input.
- loadgenerator é explicitamente excluído do catálogo RCA.

## O que de fato é “shared”

O contrato semântico é compartilhado; não existe uma biblioteca executável única entre Go, Node, .NET, Python e Java. Cada runtime possui wrappers/adapters próprios. Em vários caminhos, o mesmo interceptor convencional mede duração/status e chama um callback OTel. Em Node, dispatchObservation chama adapters. Em cartservice, uma conclusão Redis alimenta Activity/metrics convencionais/OTel e o evento. São caminhos próximos da origem, mas o fato de existir uma função chamada recordShared... não prova que todas as boundaries ou imagens executadas usem essa versão.

## Estado importante

A matriz estática atual diz 10/10 COMPLETE_WITH_NA, porém o artifact de cobertura de faults ainda marca F1/F4/F5 como bloqueados e F2/F3 parciais. A matriz é uma inspeção textual de código/configuração e não atesta build, imagem, rollout ou evento sobrevivendo ao backend. Ver finding OBS-001.

