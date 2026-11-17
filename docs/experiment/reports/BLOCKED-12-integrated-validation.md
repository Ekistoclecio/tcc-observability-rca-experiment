# BLOCKED — Etapa 12: validação integrada interrompida em F2

## Estado

- Branch: `experiment-v2`
- Commit executado: `7c1c1f8f1a076a9a10cef0afcfc83417a7c91fb5`
- Proveniência: 10/10 imagens e Pods passaram no preflight para esse commit.
- Resultado: F1 passou; F2 falhou no gate A e a bateria foi interrompida. F3–F5 não foram executados nesta bateria.
- A tentativa anterior F1 inválida permanece separada em `BLOCKED-12-artifacts-1794863260/`.
- Evidências desta bateria: `BLOCKED-12-artifacts-1794866601/`.

## Observado

O Ground Truth independente de F2 registrou a janela confirmada `2026-11-16T22:02:39.064404Z`–`2026-11-16T22:02:55.311984Z` e a aresta `checkoutservice -> paymentservice`. O Chaos Mesh marcou `Selected=True` e `AllInjected=True`. O Chaos Daemon registrou configuração netem de 100 ms no namespace de rede do Pod checkoutservice e classificação para o IP do Pod paymentservice `10.244.0.37`.

O fluxo normal durante a janela concluiu `POST /cart/checkout` com HTTP 200. No RAW A, a span cliente `checkoutservice -> PaymentService/Charge` observada nessa janela durou `5,866 ms`, abaixo do limite de 100 ms. No RAW B, não foi encontrada uma span cliente checkoutservice para paymentservice; uma span server `PaymentService/Charge` existe em B, mas isso não prova a duração da aresta nem explica a ausência do span cliente. O validator interrompeu em `F2/A`; o critério A/B não passou.

Depois da limpeza: 11/11 componentes do SUT estavam Ready e não havia `NetworkChaos`/`StressChaos` residual. O cluster `tcc-baseline` não foi alterado.

## Primeira divergência e causa

A primeira divergência observável é entre confirmação/configuração do fault no plano de controle e a evidência da operação: a span A medida não mostra o atraso configurado. Em B, há também uma ausência de span cliente na coleta.

**Root cause: UNKNOWN / NOT_PROVEN.** Os logs do Chaos Daemon comprovam que ele registrou o pedido/configuração de netem e as operações de setup/cleanup; não preservam contadores de qdisc ou prova por pacote de que a conexão efetiva para o Pod target casou com o filtro. Os RAWs não determinam se o atraso não afetou esse fluxo, se a primeira chamada foi encaminhada por outro endereço/caminho, ou se a observação foi perdida. A ausência B precisa de diagnóstico separado na exportação/consulta de traces.

## Invariante afetado

F2 deve produzir evidência observável de latência na aresta lógica `checkoutservice -> paymentservice` nos dois cenários. Confirmação de recurso Chaos não substitui confirmação do efeito observado nem cobertura de telemetria.

## Evidências

- `F2/ground-truth.json`: GT e janela confirmada independentes.
- `F2/fault-smoke.json`: workload normal terminou durante F2; `Selected`/`AllInjected`; recuperação confirmada.
- `F2/raw/datasetA/traces/checkoutservice.json`: span cliente A de 5,866 ms.
- `F2/raw/datasetB/traces/checkoutservice.json`: nenhuma span cliente da aresta no RAW coletado.
- `f2-chaos-daemon-readonly.log`: registro do pedido netem `100ms`, ipset para `10.244.0.37/32`, comandos de configuração e limpeza; SHA-256 está em `f2-readonly-diagnosis.json`.
- `f2-readonly-diagnosis.json`: resumo estruturado das observações e das provas ausentes.

## Correção e próxima ação

Nenhuma correção automática é segura: não há causa comprovada para escolher entre configuração/elegibilidade de tráfego do NetworkChaos, efeito real de rede ou perda/consulta de traces B. Não mudar limiar, semântica do fault, validator, instrumentação ou configuração para fazer a execução passar.

A próxima investigação deve determinar, antes de uma nova execução física, como provar a correspondência efetiva de pacotes/qdisc no caminho do Pod source para o Pod target e por que a span cliente B não chegou ao RAW. Depois de uma hipótese concreta e uma correção autorizada, uma nova validação F2 física será necessária. A bateria atual não pode ser retomada ou combinada com outra.

- Safe automatic correction possible: **NO**
- New physical validation required after diagnosis: **YES**
- Working tree contains only the blocker report/evidence added after the run; implementation remains at the tested commit.
- Stopped before: **F3**.
