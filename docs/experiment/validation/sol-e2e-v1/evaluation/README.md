# Avaliação piloto determinística RCA — v1

Esta é uma avaliação piloto de cinco faults físicos pareados, uma execução por fault. Não sustenta conclusões estatísticas generalizáveis. Nenhum LLM-as-a-Judge, teste de significância ou avaliação qualitativa foi aplicado. Tokens, custo e latência são operacionais; `delta = B - A` e sinal positivo não significa automaticamente melhor.

Evaluator commit: `96515afb13e9fa5e01a8f9e43cf7e3cd03e07759`<br>
Predictions commit: `cba1569e007cbda3921f061dbcbbbeb9e65d7452`<br>
Ground Truth mapping and artifact integrity: `PASS`

## Métricas diagnósticas

| Treatment | Cases | Acc@1 | Acc@3 | MRR | Location Acc. | Fault Type Acc. |
|---|---:|---:|---:|---:|---:|---:|
| A | 5 | 0.6 | 0.6 | 0.6 | 0.6 | 0.8 |
| B | 5 | 0.6 | 0.6 | 0.6 | 0.6 | 0.8 |
| Δ B−A | — | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |

## Consumo por treatment

| Treatment | Input | Cached input | Output | Reasoning | Total | Cost USD | Mean latency ms | Median latency ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 141907 | 0 | 3101 | 659 | 145008 | 0.38577 | 11034.0088 | 11412.634 |
| B | 193430 | 0 | 2874 | 394 | 196304 | 0.5123075 | 9299.6672 | 9277.129 |
| Δ B−A | 51523.0 | 0.0 | -227.0 | -265.0 | 51296.0 | 0.1265375 | -1734.3416 | -2135.505 |

## Resultados pareados por fault

Os campos de qualidade usam métricas do caso; `Δ` é sempre B−A.

| Fault | Treatment | Rank correto | Acc@1 | Acc@3 | RR | Location | Type | Tokens | Cost USD | Latency ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| F2 | A | 1 | 1 | 1 | 1.0 | 1 | 1 | 33894 | 0.0894135 | 10874.941 |
| F2 | B | 1 | 1 | 1 | 1.0 | 1 | 1 | 45903 | 0.119376 | 9429.197 |
| F1 | A | — | 0 | 0 | 0.0 | 0 | 0 | 33635 | 0.0890585 | 11412.634 |
| F5 | B | 1 | 1 | 1 | 1.0 | 1 | 1 | 21761 | 0.0582635 | 8007.313 |
| F4 | A | — | 0 | 0 | 0.0 | 0 | 1 | 30850 | 0.081826 | 11990.717 |
| F3 | A | 1 | 1 | 1 | 1.0 | 1 | 1 | 28632 | 0.076386 | 11947.542 |
| F4 | B | — | 0 | 0 | 0.0 | 0 | 1 | 42715 | 0.1111135 | 9019.314 |
| F3 | B | 1 | 1 | 1 | 1.0 | 1 | 1 | 40693 | 0.105541 | 9277.129 |
| F1 | B | — | 0 | 0 | 0.0 | 0 | 0 | 45232 | 0.1180135 | 10765.383 |
| F5 | A | 1 | 1 | 1 | 1.0 | 1 | 1 | 17997 | 0.049086 | 8944.21 |

## Proveniência e limites

A unidade pareada é a mesma execução física, identificada pelo Ground Truth independente. Location exige igualdade exata de kind e componentes, incluindo direção da aresta. Accuracy@k requer location e fault type corretos simultaneamente; Location Accuracy e Fault Type Accuracy usam somente rank 1. Não foram feitas normalizações fuzzy.

Nenhuma API foi chamada nesta etapa. Ground Truth foi usado somente pelo evaluator. Não foram calculados Evidence Precision, Hallucination Rate, p-values, intervalos de confiança ou métricas por LLM judge.
