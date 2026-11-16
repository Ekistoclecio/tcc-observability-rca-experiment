# INCIDENT — Jaeger A coleta dados de B por colisão de porta local

## Estado

**Causa raiz comprovada; correção no coletor commitada; os bundles históricos foram reconsultados e a cobertura foi revalidada.** O incidente invalida a origem dos traces A nos bundles RAW originais da Etapa 8 e nas primeiras coletas F4/F5 da Etapa 9. Não invalida as métricas, os logs, a execução do workload nem o Ground Truth dessas coletas.

## Observado e esperado

O coletor anterior usava `127.0.0.1:16686` para Jaeger A e `127.0.0.1:16687` para Jaeger B. Ele descartava stdout/stderr dos processos `kubectl port-forward` e tratava uma resposta HTTP de readiness na porta local como prova suficiente de que o encaminhamento pretendido havia sido estabelecido.

Durante a investigação, o processo de port-forward de Jaeger B já escutava em `127.0.0.1:16686`. O port-forward de Jaeger A estava disponível em `16688`. As consultas nativas gravadas nos manifests antigos apontam Jaeger A para `16686` e Jaeger B para `16687`; como o forward A não conseguia ocupar a porta, a readiness local respondia através do forward B preexistente. Portanto, os traces rotulados A vieram do backend B.

Evidência de processo/socket capturada durante a investigação:

| Porta local | Processo/serviço observado |
|---:|---|
| 16686 | `kubectl ... -n observability-b port-forward service/jaeger-b 16686:16686` (PID 650800 na captura) |
| 16687 | outro `kubectl port-forward` de `service/jaeger-b` |
| 16688 | `kubectl port-forward -n observability-a service/jaeger-a 16688:16686` |

As diferenças nos traces consultados diretamente e por encaminhamentos distintos confirmaram a origem: na janela normal, a resposta efetiva de Jaeger A tem 12 trace IDs/310 spans únicos; B tem 15/317. Em F4, A apresenta um span de erro `paymentservice/Charge`, enquanto B tem três spans de paymentservice no recorte; em F5, A tem cinco spans de erro de `cartservice` para Redis e B tem seus próprios registros correlacionados. Essas respostas foram obtidas por consultas históricas com o coletor corrigido, sem novos requests ao SUT.

## Primeira divergência e causa

```text
porta esperada por Jaeger A (16686)
→ já ocupada pelo port-forward de Jaeger B
→ falha de bind do port-forward A não era observada
→ readiness consultava o serviço que já respondia na porta
→ respostas B eram armazenadas sob o cenário A
```

**Root cause: PROVEN.** Não foi falha de isolamento entre os serviços Jaeger no cluster; foi uma colisão de porta na máquina coletora, combinada com a ausência de confirmação do processo local. A e B continuam em namespaces/serviços distintos.

## Correção

Commit `265aa87039964a412773a9046b7bba65f587c76f` (`fix(collection): verify unique backend port forwards`): o coletor reserva uma porta local efêmera para cada backend, captura a saída de cada processo, exige confirmação da associação exata porta local→porta remota e verifica que o processo segue ativo durante readiness. O manifesto registra serviço, namespace, porta local e PID do processo criado pelo coletor. Os processos preexistentes não são encerrados.

Testes do coletor: 6/6 passaram, incluindo confirmação de associação local/remota. A validação semântica e a verificação de integridade dos três bundles corrigidos também passaram.

## Evidência corrigida e preservação

Os bundles originais permanecem inalterados e com hashes originais. Eles continuam nos caminhos originais, mas seus arquivos de trace A são evidência inválida de A e devem ser lidos somente como registro histórico do incidente. Não houve edição manual de qualquer resposta RAW.

As novas consultas byte-preservadas e hash-verificáveis estão em:

- `docs/experiment/stages/08-raw-smoke/raw-corrected/` — mesma janela da execução normal registrada, sem novo workload;
- `docs/experiment/stages/09-raw-semantic-validation/raw/corrected/F4/` — mesma janela F4 confirmada pelo control plane;
- `docs/experiment/stages/09-raw-semantic-validation/raw/corrected/F5/` — mesma janela F5 confirmada pelo control plane.

Cada manifesto corrigido registra o commit do coletor, SHA-256 do script, portas locais exclusivas, serviços encaminhados, janela, fingerprint da imagem de aplicação e hashes. Os bundles foram conferidos com `collect_raw.py --verify-existing` e com o verificador independente da Etapa 9.

## Impacto metodológico e alcance

- Os traces A dos bundles originais de Etapa 8/F4/F5 **não podem sustentar paridade nem cobertura A**.
- Métricas e logs estavam em portas distintas e foram consultados nos backends corretos. Seus counts nas janelas permaneceram 217/1.375 séries e 152/136 logs na janela normal; F4 263/1.903 séries e 73/64 logs; F5 298/2.063 séries e 89/48 logs.
- O workload normal e as smokes F4/F5 ocorreram uma única vez cada. A correção reaplicou apenas consultas read-only às janelas já registradas.
- As verificações semânticas da Etapa 9 foram executadas sobre os bundles corrigidos, não sobre os traces A inválidos.
- Não houve mudança em instrumentação, backends, fault, Ground Truth ou dados de aplicação para fabricar paridade.

## Estado operacional

Os port-forwards persistentes existentes foram deixados intactos. Os novos processos transitórios do coletor foram encerrados pelo próprio coletor. Nenhum fault, workload adicional ou alteração de backend ocorreu durante a correção.
