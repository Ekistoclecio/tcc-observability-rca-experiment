# BLOCKED — Etapa 12 integrated validation (bateria invalidada em F1)

## Identificação

- Branch: `experiment-v2`.
- Commit executado: `5ab52334b08d1ef1d93bf17c0dad9c5593c2b236`.
- Proveniência Git → imagem → Pod: PASS, 10/10.
- Preflight: PASS; ambiente, Scenario A/B, SUT e ausência de faults residuais foram confirmados.
- A tentativa anterior no commit `e7305f6a` também foi invalidada antes de qualquer fault por erro no entrypoint de F1. Seu diagnóstico e artefatos permanecem registrados no histórico deste blocker.

## Observado nesta tentativa

F1 foi selecionada e injetada pelo Chaos Mesh no Pod `checkoutservice-568b456645-gbxfs`, e o runner registrou recuperação. O Chaos Daemon registrou o início do `stress-ng` às `20:51:35.796Z` e seu encerramento às `20:51:40.302Z`. O fluxo ativo completou com HTTP 200.

O Ground Truth confirmou a janela `20:51:36.926493Z`–`20:51:40.492123Z`, total de **3,56563 s**. Os Prometheus A e B estão configurados com `scrape_interval: 5s`; a coleta avalia a consulta a cada 1 s, o que repete o último sample quando não há novo scrape. No RAW de F1, os quatro pontos de avaliação de ambos os cenários mantêm os contadores `container_cpu_usage_seconds_total` do container `server` e do cgroup do Pod constantes. O Dataset calculou `checkoutservice.cpu_utilization = 0` em A e B. O baseline normal tinha `0,009623 cores` em ambos.

## Primeira divergência e causa

- Esperado: a janela confirmada de F1 conter amostras cAdvisor suficientes para observar o delta de CPU durante o stress.
- Primeira divergência: **janela temporal de observação F1 → scrape cAdvisor**; o fault confirmado durou menos que um período de scrape.
- Causa-raiz: **PROVEN**. `fault_smoke` contava `active_seconds` desde antes da criação/confirmação do recurso, mas o Ground Truth iniciava somente após `AllInjected`. Assim, os 5 s configurados produziram uma janela confirmada de 3,56563 s, menor que o scrape de 5 s. A avaliação Prometheus de 1 s não cria samples novos; os valores RAW repetidos e o delta zero são consistentes com essa janela sem novo scrape observável.
- Impacto: a bateria é inválida; F2–F5 não foram iniciadas e nenhum resultado desta tentativa é oficial.

## Correção mínima

A correção permanece na temporização do runner, sem alterar a fault, a instrumentação ou o Builder:

1. iniciar a contagem de `active_seconds` imediatamente após confirmação da injeção;
2. exigir janela confirmada mínima de 15 s na bateria integrada, cobrindo três períodos configurados de scrape cAdvisor de 5 s.

A correção está preparada no código local para validação, junto com uma checagem de que o runner rejeita janelas inferiores a 15 s. A bateria completa deve ser repetida desde F1 somente após commit, working tree limpa, build/deploy e proveniência do novo SHA. Não executar retry seletivo de F1.

## Cleanup e evidência

- Nenhum `StressChaos` ou `NetworkChaos` residual após F1.
- SUT: 11/11 Ready na verificação posterior.
- F2–F5: não executadas.
- LLM: não executado.
- RAW, workload, Ground Truth, Dataset, RCA Input e proveniência da tentativa invalidada estão preservados em [`BLOCKED-12-artifacts-1794862330/`](BLOCKED-12-artifacts-1794862330/).

## Resultado

**BLOCKED** até a correção de temporização ser commitada e uma nova bateria completa, com proveniência correspondente, passar.
