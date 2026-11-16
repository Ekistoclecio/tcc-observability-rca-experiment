# Etapa 2 — Workload determinístico

## Estado e commit

- Branch: `experiment-v2`.
- Commit de entrada: `864dcbe26dbb69e46d1d9a425e1c97e383e18886`.
- Commit correspondente: `feat(workload): add deterministic experiment workload` (este registro é incluído nesse commit).
- Faults e Chaos não foram executados.

## Implementação

O workload está em `scripts/experiment/workload.py` e é executado no host, fora do SUT. A configuração versionada está em `config/experiment/workload.json`; não há dependências Python externas.

Cada fluxo usa um cookie jar próprio e realiza, nesta ordem:

```text
GET /
GET /product/OLJCESPC7Z
POST /cart
GET /cart
POST /cart/checkout
```

O formulário de checkout é fixo e válido para o demo. Não contém identificador de execução, fault ou cenário. O script não define nem propaga headers experimentais; os únicos headers adicionados por ele são os necessários ao POST (`Content-Type`). O cookie de sessão é gerado pelo comportamento normal do frontend. As credenciais de pagamento são os dados sintéticos de demonstração da aplicação.

Configuração validada:

| Parâmetro | Valor |
|---|---:|
| Warm-up | 1 fluxo |
| Steady state | 3 fluxos |
| Intervalo de chegada | 2 s (0,5 fluxo/s) |
| Concorrência máxima | 2 fluxos |
| Timeout por request | 10 s |
| Drain máximo configurado | 60 s |

Os fluxos steady são agendados em intervalos fixos, sem reduzir a taxa em resposta à latência. O workload aguarda os fluxos iniciados terminarem antes de encerrar. Warm-up, steady e drain são separados no resumo JSON emitido pelo script. Cada etapa exige uma resposta HTTP 200; um fluxo incompleto faz o comando retornar falha.

## Validação

Verificações estáticas:

- `python3 -m py_compile scripts/experiment/workload.py` — PASS.
- `python3 scripts/experiment/workload.py --help` — PASS.
- `python3 -m json.tool config/experiment/workload.json` — PASS.
- `git diff --check` — PASS.

Validação física sem fault, em 16/11/2026, com o ambiente da Etapa 1 e `tcc-baseline` temporariamente parado:

| Rodada | Janela UTC | Warm-up | Steady | Requests esperados | Resultado |
|---|---|---:|---:|---:|---|
| 1 | 11:41:17.365619Z–11:41:22.157607Z | 1/1 | 3/3 | 20 | PASS |
| 2 | 11:41:26.673152Z–11:41:31.064513Z | 1/1 | 3/3 | 20 | PASS |

Todos os requests dos dois ciclos retornaram HTTP 200, incluindo o checkout. A verificação prévia do ambiente retornou `GET /` HTTP 200; os serviços estavam Ready. As rodadas reutilizaram a mesma URL local via port-forward e foram direcionadas à mesma aplicação em execução. O cluster experimental foi removido ao final e o container baseline existente foi restaurado; node, kube-proxy e CoreDNS voltaram a Ready.

O script envia somente operações e payloads normais da loja. Não usa o `loadgenerator` como fonte de evidência e não envia `run_id`, `fault_id`, `scenario` ou `experiment_id`.

## Critérios

- [x] Fluxo normal inclui navegação, carrinho e checkout.
- [x] Duas rodadas curtas concluíram com sucesso.
- [x] Estrutura e configuração de carga foram iguais nas duas rodadas.
- [x] Sessões/carrinhos isolados por fluxo.
- [x] Carga steady com intervalo de chegada fixo e drenagem dos fluxos.
- [x] Nenhum metadado de controle experimental é enviado ao SUT.
- [x] Workload executado fora do SUT; loadgenerator não é usado como evidência RCA.

## Limitações conhecidas

- A validação de repetibilidade compara configuração e estrutura do fluxo; não exige igualdade byte a byte de contagens, latências ou identificadores de sessão.
- O endpoint local é fornecido por port-forward e o script aceita outro `--base-url`; a criação do cluster e do port-forward permanecem responsabilidades da camada de ambiente.
- O encerramento após timeout de drenagem pode aguardar requests já em execução terminarem, limitados pelo timeout por request.

## Resultado

**PASS** — duas rodadas sem fault completaram o mesmo fluxo de negócio com configuração fixa e sem metadados experimentais nas requests.
