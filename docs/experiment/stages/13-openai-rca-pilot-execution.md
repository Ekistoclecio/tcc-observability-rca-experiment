# Etapa 13B — execução piloto OpenAI RCA

## Escopo e congelamento

O protocolo `llm-rca-pilot-v1` foi executado após o runner estar commitado e com a árvore limpa. O conjunto oficial permaneceu congelado: cinco instâncias físicas, dois tratamentos e dez RCA Inputs. O runner não abriu Ground Truth; não houve cálculo de métricas nem comparação A/B.

## Runner e controle de compatibilidade

- Commit do runner: `17e37b7fd07720f1e10cad52865d7974d102eb79`.
- SDK OpenAI: `3.51.0`; execução serial (`parallelism=1`).
- O `.env` da raiz estava ignorado e não rastreado. A credencial foi validada por presença, nunca impressa ou persistida; o scan de secret nos artifacts passou.
- O smoke sintético separado concluiu (`COMPLETED`) com uma chamada lógica e uma tentativa. Modelo e service tier retornados corresponderam a `gpt-6-luna` e `default`. `included_in_pilot_results=false`.

## Protocolo e integridade

- Prompt `rca-prompt-v1`: SHA-256 `2e107e3c7f45b843bdad435f8c233dfe5085f3589c56ddd2bf04da12998fa86b`.
- Schema `rca-schema-v1`: SHA-256 canônico `00f6a92d2946e1e9d2482eb75e69e7244763562b422d73f35b3cb6b6a3c540c7`.
- Plano cego: SHA-256 `31d2472822e807e07fda3b5c2156bc001fdc51515a5c87ce227ad06b3f77089b`.
- Os dez hashes dos inputs congelados, hashes de prompt/schema/protocolo, fingerprints dos requests e manifest de hashes dos outputs foram verificados.

## Resultado técnico do piloto

As dez chamadas lógicas foram concluídas com status `COMPLETED`: dez respostas estruturadas e semanticamente válidas, zero recusas, incompletas, respostas inválidas ou falhas de transporte. Houve dez tentativas no total, sem retry.

- Tokens: 337.838 input; 0 cached input; 10.844 output; 5.998 reasoning; 348.682 total.
- Custo estimado pelo snapshot `pricing-v1`: USD 0,047651.
- Latência limpa: 10 casos; mediana 11.308,137 ms; p95 14.792,032 ms.
- Resultado e predictions: `docs/experiment/llm/pilot-v1/`.

O piloto valida o pipeline. Não constitui evidência estatística generalizável. Ground Truth, evaluator, métricas RCA e comparação A/B permanecem fora desta etapa e não foram executados.
