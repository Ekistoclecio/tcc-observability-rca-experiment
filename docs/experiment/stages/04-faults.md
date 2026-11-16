# Etapa 4 — Injeção de F1–F5 e recuperação

## Estado e revisão validada

- Branch: `experiment-v2`.
- Implementação de faults iniciada em `cc5d1f25893389c75585758dff5d696a145d1276`.
- Smoke física final de F1–F5 e proveniência executadas no commit `23cab204d79f31e82322c987b3486131cf8f7af4`.
- Imagens dos dez serviços, tags e labels OCI apontaram para `23cab204d79f31e82322c987b3486131cf8f7af4`; o verificador de runtime passou 10/10 antes de cada smoke.
- Este registro foi preparado depois das execuções e não altera a revisão nelas executada.

## Implementação

O runner `scripts/experiment/fault_smoke.py` executa uma fault por vez, exige ambiente Kind exclusivo, proveniência das dez imagens e SUT pronto, registra confirmação e recuperação pelo control plane e verifica um fluxo normal após a recuperação. Ground Truth é produzido pelo estado do mecanismo de injeção, não por telemetria.

Mecanismos utilizados:

| Fault | Mecanismo e localização | Confirmação de injeção |
|---|---|---|
| F1 | Chaos Mesh `StressChaos` em `checkoutservice` | `Selected` e `AllInjected` |
| F2 | Chaos Mesh `NetworkChaos` somente na aresta `checkoutservice -> paymentservice` | `Selected` e `AllInjected` |
| F3 | Escala `paymentservice` para zero | réplicas desejadas, Pods e endpoints prontos iguais a zero |
| F4 | Flag de arquivo em `paymentservice/Charge` | flag presente no Pod pronto |
| F5 | Escala `redis-cart` para zero | réplicas desejadas, Pods e endpoints prontos iguais a zero |

## Evidência das smokes

Todas as cinco smokes finais foram executadas no mesmo commit `23cab204d79f31e82322c987b3486131cf8f7af4`. Todas tiveram precondição normal PASS, injeção confirmada, Ground Truth elegível e `RECOVERED`, probe durante a fault PASS com os status esperados, e probe normal pós-recuperação PASS.

| Fault | Evidência durante a fault | Recuperação |
|---|---|---|
| F1 `cpu_saturation` | Chaos Mesh selecionou e injetou stress no Pod de `checkoutservice`; fluxo completo HTTP 200 | recurso Chaos removido; fluxo pós-recuperação HTTP 200 |
| F2 `network_latency` | Chaos Mesh selecionou `checkoutservice` e o alvo `paymentservice`; fluxo completo HTTP 200 sob a latência configurada | recurso Chaos removido; fluxo pós-recuperação HTTP 200 |
| F3 `service_unavailable` | `paymentservice`: desired replicas=0, Pods=0, ready endpoints=0; `POST /cart/checkout` retornou HTTP 500 | deployment/endpoints restaurados; fluxo normal passou após 3 probes, acomodando a reconexão do canal gRPC persistente |
| F4 `application_exception` | flag presente no Pod de `paymentservice`; checkout retornou HTTP 500 | flag ausente no mesmo Pod, identidade e restart count preservados; fluxo pós-recuperação HTTP 200 |
| F5 `datastore_unavailable` | `redis-cart`: desired replicas=0, Pods=0, ready endpoints=0; as cinco rotas do fluxo que consultam/modificam o carrinho retornaram HTTP 500 | deployment/endpoints restaurados; fluxo normal pós-recuperação HTTP 200 |

Os status de HTTP são verificações da consequência funcional do mecanismo nesta etapa. Não são usados como Ground Truth nem como seleção de evidência diagnóstica.

## Correções realizadas durante a etapa

- F3 espera a remoção dos Pods, além de réplicas e endpoints zerados, antes de declarar indisponibilidade. Evidência inicial mostrou que um Pod ainda em terminação podia atender uma chamada por canal gRPC já aberto.
- Respostas vazias do Kubernetes para listas de Pods e `EndpointSlice.endpoints` são tratadas como listas vazias.
- Após restaurar um serviço, o runner faz probes normais limitados e registrados até observar um fluxo funcional; readiness do Pod, por si só, não comprova reconexão imediata de canais gRPC persistentes.
- F5 espera HTTP 500 em todas as rotas do fluxo que realmente consultam o carrinho. Inspeção do código e logs mostrou que a página inicial e a página de produto também chamam `GetCart`; essa expectativa representa propagação natural da indisponibilidade de Redis, sem mudar o fault ou o workload.

Esses ajustes alteram apenas o controle/validação de faults. Não alteram o desenho experimental, a semântica dos mecanismos, a aplicação nem a observabilidade.

## Limpeza e validações

- [x] F1–F5: injeção confirmada e Ground Truth `RECOVERED` (5/5).
- [x] F1–F5: probes de impacto e recuperação PASS (5/5).
- [x] Proveniência Git → imagens → Pods: 10/10 no SHA final.
- [x] Suíte Python de `tests/experiment`: 27 testes PASS.
- [x] `py_compile`, `bash -n` dos scripts de ambiente/deploy e `git diff --check`: PASS.
- [x] SUT: 11/11 deployments disponíveis e Pods prontos ao final.
- [x] Nenhum `StressChaos` ou `NetworkChaos` residual.
- [x] Flag F4 ausente; `paymentservice` sem restart durante F4.
- [x] `redis-cart` restaurado com uma réplica pronta.
- [x] Somente `tcc-experiment-v2-stage1` está ativo; `tcc-baseline` permaneceu parado e não foi removido.

## Evidências de execução

Os registros completos das smokes, incluindo timestamps, Ground Truth, evidência do control plane e validação de proveniência, foram preservados durante esta sessão em `/tmp/tcc-experiment-v2-stage4/`. Não são arquivos de resultado final do experimento e não foram adicionados ao repositório.

## Limitações

- Esta etapa valida injeção, impacto funcional, Ground Truth, recuperação e limpeza. Ainda não valida cobertura/qualidade de métricas, logs ou traces.
- F1/F2 não precisam causar falha HTTP para que a injeção esteja confirmada; a avaliação de evidência observacional pertence às etapas seguintes.

## Resultado

**PASS** — as cinco faults foram aplicadas e recuperadas, o cleanup foi confirmado e as imagens executadas corresponderam ao commit SHA `23cab204d79f31e82322c987b3486131cf8f7af4`.
