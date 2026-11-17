# Avaliação piloto determinística RCA — v1

Esta é uma avaliação piloto de cinco faults físicos pareados, uma execução por fault. Não sustenta conclusões estatísticas generalizáveis. Nenhum LLM-as-a-Judge, teste de significância ou avaliação qualitativa foi aplicado. Tokens, custo e latência são operacionais; `delta = B - A` e sinal positivo não significa automaticamente melhor.

Evaluator commit: `273007d9df6aaab4ad1b24ae7381c3043c3544df`<br>
Predictions commit: `552c114029c66cbd2aa9309757876f69907b2cba`<br>
Ground Truth mapping and artifact integrity: `PASS`

## Métricas diagnósticas

| Treatment | Cases | Acc@1 | Acc@3 | MRR | Location Acc. | Fault Type Acc. |
|---|---:|---:|---:|---:|---:|---:|
| A | 5 | 0.2 | 0.2 | 0.2 | 0.2 | 0.6 |
| B | 5 | 0.4 | 0.4 | 0.4 | 0.4 | 0.8 |
| Δ B−A | — | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 |

## Consumo por treatment

| Treatment | Input | Cached input | Output | Reasoning | Total | Cost USD | Mean latency ms | Median latency ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 144757 | 0 | 5927 | 3310 | 150684 | 0.02105775 | 12302.1534 | 12596.485 |
| B | 193081 | 0 | 4917 | 2688 | 197998 | 0.02659325 | 10214.626 | 9288.715 |
| Δ B−A | 48324.0 | 0.0 | -1010.0 | -622.0 | 47314.0 | 0.0055355 | -2087.5274 | -3307.77 |

## Resultados pareados por fault

Os campos de qualidade usam métricas do caso; `Δ` é sempre B−A.

| Fault | Treatment | Rank correto | Acc@1 | Acc@3 | RR | Location | Type | Tokens | Cost USD | Latency ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| F1 | A | — | 0 | 0 | 0.0 | 0 | 0 | 33950 | 0.004656925 | 12099.676 |
| F1 | B | — | 0 | 0 | 0.0 | 0 | 0 | 46463 | 0.00632755 | 14792.032 |
| F2 | A | — | 0 | 0 | 0.0 | 0 | 0 | 34427 | 0.004818175 | 13587.461 |
| F2 | B | — | 0 | 0 | 0.0 | 0 | 1 | 46329 | 0.006160425 | 10516.598 |
| F3 | A | — | 0 | 0 | 0.0 | 0 | 1 | 29130 | 0.0040743 | 12596.485 |
| F3 | B | 1 | 1 | 1 | 1.0 | 1 | 1 | 41042 | 0.005418925 | 8281.572 |
| F4 | A | — | 0 | 0 | 0.0 | 0 | 1 | 31917 | 0.004487175 | 13842.017 |
| F4 | B | — | 0 | 0 | 0.0 | 0 | 1 | 42701 | 0.005651425 | 8194.213 |
| F5 | A | 1 | 1 | 1 | 1.0 | 1 | 1 | 21260 | 0.003021175 | 9385.128 |
| F5 | B | 1 | 1 | 1 | 1.0 | 1 | 1 | 21463 | 0.003034925 | 9288.715 |

## Proveniência e limites

A unidade pareada é a mesma execução física, identificada pelo Ground Truth independente. Location exige igualdade exata de kind e componentes, incluindo direção da aresta. Accuracy@k requer location e fault type corretos simultaneamente; Location Accuracy e Fault Type Accuracy usam somente rank 1. Não foram feitas normalizações fuzzy.

Nenhuma API foi chamada nesta etapa. Ground Truth foi usado somente pelo evaluator. Não foram calculados Evidence Precision, Hallucination Rate, p-values, intervalos de confiança ou métricas por LLM judge.
