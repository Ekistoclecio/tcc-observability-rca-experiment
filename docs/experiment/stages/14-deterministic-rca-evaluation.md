# Etapa 14 — avaliação determinística piloto RCA

## Escopo e proveniência

As dez predictions congeladas foram avaliadas por código determinístico, sem chamada de API ou LLM-as-a-Judge. O evaluator foi commitado antes de pontuar as predictions (`273007d9df6aaab4ad1b24ae7381c3043c3544df`). O runner e as predictions vieram, respectivamente, dos commits `17e37b7fd07720f1e10cad52865d7974d102eb79` e `552c114029c66cbd2aa9309757876f69907b2cba`.

Integridade passou para os inventários de `results/` (1.081 entradas), `teste/` (27), e `llm/pilot-v1/` (53). Os dez hashes dos RCA Inputs, prompt, schema, protocolo e execution plan conferem. Foram confirmadas dez respostas `COMPLETED`, com validação estrutural e semântica `PASS`, uma tentativa por caso e latência limpa. O mapeamento privado vinculou cada input ao tratamento por seu artifact de origem; cinco Ground Truths elegíveis foram associados e cruzados com os registros de fault do pacote independente `teste/`.

## Regras de pontuação congeladas

- Location é igualdade exata de `kind` e componentes; direção de `service_edge` é significativa. Não há fuzzy match nem aliases.
- Uma hipótese conta para Accuracy@1/@3 e MRR somente quando location **e** fault type coincidem. MRR usa o primeiro rank completamente correto.
- Location Accuracy e Fault Type Accuracy usam somente rank 1 e são medidas separadamente.
- Tokens, custo e latência vêm dos artifacts do piloto. Reasoning tokens não são somados novamente ao total; o custo congelado não foi recalculado.
- Deltas são sempre `B - A`. Valores positivos indicam maior valor da métrica, não automaticamente melhor resultado.

## Resultados por tratamento

| Métrica | A | B | Δ B−A |
|---|---:|---:|---:|
| Casos | 5 | 5 | — |
| Accuracy@1 | 0,2 | 0,4 | +0,2 |
| Accuracy@3 | 0,2 | 0,4 | +0,2 |
| MRR | 0,2 | 0,4 | +0,2 |
| Location Accuracy | 0,2 | 0,4 | +0,2 |
| Fault Type Accuracy | 0,6 | 0,8 | +0,2 |

| Consumo | A | B | Δ B−A |
|---|---:|---:|---:|
| Input tokens | 144.757 | 193.081 | +48.324 |
| Cached input tokens | 0 | 0 | 0 |
| Output tokens | 5.927 | 4.917 | −1.010 |
| Reasoning tokens | 3.310 | 2.688 | −622 |
| Total tokens | 150.684 | 197.998 | +47.314 |
| Estimated cost (USD) | 0,02105775 | 0,02659325 | +0,00553550 |
| Mean latency (ms) | 12.302,1534 | 10.214,626 | −2.087,5274 |
| Median latency (ms) | 12.596,485 | 9.288,715 | −3.307,77 |

## Resultados pareados

| Fault | A rank correto | B rank correto | Acc@1 A/B | Acc@3 A/B | RR A/B | Location A/B | Type A/B |
|---|---:|---:|---:|---:|---:|---:|---:|
| F1 | — | — | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| F2 | — | — | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 1 |
| F3 | — | 1 | 0 / 1 | 0 / 1 | 0 / 1 | 0 / 1 | 1 / 1 |
| F4 | — | — | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 1 / 1 |
| F5 | 1 | 1 | 1 / 1 | 1 / 1 | 1 / 1 | 1 / 1 | 1 / 1 |

O breakdown completo de cada caso, incluindo tokens, custo, latência e deltas operacionais, está em `evaluation/pilot-v1/paired-comparison.json` e nas dez entradas `cases/`.

## Interpretação e limites

Neste dataset piloto, B teve valores maiores nas cinco métricas diagnósticas agregadas; também usou mais tokens de entrada e custo estimado, e apresentou menor latência média. Os resultados por fault não são uniformes: F3 foi completamente correto em B e não em A; F5 foi correto em ambos; os demais não tiveram diagnóstico completo correto. Essas observações descrevem somente as cinco execuções físicas pareadas presentes neste piloto.

Há cinco faults físicos, uma execução por fault. Não foram feitos testes de significância, intervalos de confiança ou alegações generalizáveis. Evidence Precision, Hallucination Rate, qualidade de explicação e julgamento semântico permanecem fora do escopo. As predictions, RCA Inputs, Ground Truth, `results/` e `teste/` não foram modificados.

Artifacts de avaliação auditáveis: [`evaluation/pilot-v1/`](../evaluation/pilot-v1/). O evaluator não acessa a rede e não executou nenhuma chamada LLM.
