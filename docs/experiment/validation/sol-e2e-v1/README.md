# Etapa 15 — piloto end-to-end GPT-6.1 Sol

## Execução física e protocolo

Validação piloto com ambiente fresco: cinco fault instances físicos, uma execução por fault, tratamentos pareados A/B. F1–F5, Ground Truth, RAW, validação semântica, Dataset A/B, RCA Input A/B, recuperação, cleanup e finalização passaram. Não é uma bateria estatística definitiva.

- Identidade física: `sol-e2e-v1-20261117T183158Z`; commit físico testado `35069e451c95969962753f2ed1370fb4c77d4c0a`.
- Cluster `tcc-experiment-v2-stage1` reconstruído; Chaos Mesh `2.8.6`.
- Provenance: `PASS 10/10`; SUT `11/11 Ready`.
- Jaeger PVCs novos A/B `c8d16fdc-6a42-422a-9357-e514fdc60099` / `2e822f25-efa6-4c0a-b0fe-51a53a743059`; 512 MiB, restart count 0/0, OOM none.
- F1–F5: `{"F1": "PASS", "F2": "PASS", "F3": "PASS", "F4": "PASS", "F5": "PASS"}`.
- Commit do runner/protocolo `0c264780a2b33213ec763e3f48b979bd154e3c7c`; predictions `cba1569e007cbda3921f061dbcbbbeb9e65d7452`; evaluator `96515afb13e9fa5e01a8f9e43cf7e3cd03e07759`.
- Protocolo `llm-rca-sol-e2e-v1`, modelo `gpt-6.1-sol`, reasoning standard/medium, tools none.
- Prompt `rca-prompt-v1` SHA-256 `2e107e3c7f45b843bdad435f8c233dfe5085f3589c56ddd2bf04da12998fa86b`; schema `rca-schema-v1` SHA-256 `00f6a92d2946e1e9d2482eb75e69e7244763562b422d73f35b3cb6b6a3c540c7`.
- Pricing Standard verificado em 2026-11-17: input USD 2/M, cached input USD 0,10/M e output USD 10/M. Acima de 272K input tokens por request aplicam-se input/cache ×2 e output ×1,5; nenhuma chamada Sol cruzou o limite. Fontes: [pricing oficial](https://developers.openai.com/api/docs/pricing) e [modelo GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

## Chamadas e custo

Uma chamada sintética de controle passou e está fora dos resultados piloto. Dez chamadas RCA sequenciais foram concluídas com dez respostas estruturadas e semanticamente válidas, zero retries e zero chamadas de evaluator/API após a inferência. O runner não carregou Ground Truth.

| Parcela | USD |
|---|---:|
| Control smoke (excluído do piloto) | 0.00613 |
| Dez diagnósticos RCA | 0.8980775 |
| Custo total Sol API | 0.9042075 |

Uso das dez chamadas: input 335337; cached input 0; output 5975; reasoning 1053 (não somado novamente); total 341312. Sem retries: mediana de latência 10097.29 ms.

## Resultados A/B determinísticos

| Tratamento | n | Acc@1 | Acc@3 | MRR | Location | Fault type | Input | Cached | Output | Reasoning | Total | Cost USD | Mean latency ms | Median latency ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 5 | 0.6 | 0.6 | 0.6 | 0.6 | 0.8 | 141907 | 0 | 3101 | 659 | 145008 | 0.38577 | 11034.0088 | 11412.634 |
| B | 5 | 0.6 | 0.6 | 0.6 | 0.6 | 0.8 | 193430 | 0 | 2874 | 394 | 196304 | 0.5123075 | 9299.6672 | 9277.129 |
| Δ B−A | — | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 51523.0 | 0.0 | -227.0 | -265.0 | 51296.0 | 0.1265375 | -1734.3416 | -2135.505 |

Neste dataset piloto, os cinco indicadores diagnósticos agregados foram iguais em A e B. B teve 51.523 tokens de entrada e 51.296 tokens totais a mais, custou USD 0,1265375 a mais e apresentou latência média 1.734,3416 ms menor. Descritivo desta execução; não indica superioridade geral.

| Fault | Tratamento | Rank correto | Acc@1 | Acc@3 | RR | Location | Type | Tokens | Custo USD | Latência ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| F1 | A | — | 0 | 0 | 0.0 | 0 | 0 | 33635 | 0.0890585 | 11412.634 |
| F1 | B | — | 0 | 0 | 0.0 | 0 | 0 | 45232 | 0.1180135 | 10765.383 |
| F2 | A | 1 | 1 | 1 | 1.0 | 1 | 1 | 33894 | 0.0894135 | 10874.941 |
| F2 | B | 1 | 1 | 1 | 1.0 | 1 | 1 | 45903 | 0.119376 | 9429.197 |
| F3 | A | 1 | 1 | 1 | 1.0 | 1 | 1 | 28632 | 0.076386 | 11947.542 |
| F3 | B | 1 | 1 | 1 | 1.0 | 1 | 1 | 40693 | 0.105541 | 9277.129 |
| F4 | A | — | 0 | 0 | 0.0 | 0 | 1 | 30850 | 0.081826 | 11990.717 |
| F4 | B | — | 0 | 0 | 0.0 | 0 | 1 | 42715 | 0.1111135 | 9019.314 |
| F5 | A | 1 | 1 | 1 | 1.0 | 1 | 1 | 17997 | 0.049086 | 8944.21 |
| F5 | B | 1 | 1 | 1 | 1.0 | 1 | 1 | 21761 | 0.0582635 | 8007.313 |

## Limites e integridade

A comparação Luna–Sol não é controlada porque usa execuções físicas e RCA Inputs diferentes; os artifacts Luna anteriores não foram alterados. A comparação principal A/B é pareada na mesma execução física. Não foram calculados p-values, significância, generalização ou LLM-as-a-Judge.

Inventários e SHA-256 físicos, dos inputs, predictions e avaliação passaram. O manifesto externo do control smoke foi completado para incluir o manifesto aninhado da tentativa; nenhum byte da resposta foi alterado. A varredura não encontrou a credencial em artifacts. Ground Truth só foi aberto após o commit das predictions.
