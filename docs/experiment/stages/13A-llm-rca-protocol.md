# Etapa 13A — protocolo piloto LLM/RCA congelado

## Escopo

Esta etapa congela o contrato e valida localmente a construção de requests e respostas. Nenhum client OpenAI foi inicializado, nenhuma API/LLM foi chamada e nenhuma execução RCA ou métrica de desempenho A/B foi calculada.

## Dataset congelado

- Commit físico testado: `35069e451c95969962753f2ed1370fb4c77d4c0a`.
- Commit de publicação dos artifacts: `29bf7073f4147b30810c8ddaff5f84a73dd9cdfb`.
- Cinco instâncias físicas, uma execução por fault, dois tratamentos e dez RCA Inputs.
- SHA-256 dos dez inputs verificados contra os bytes em `results/`; os arquivos de dataset não foram alterados.
- O pacote independente existente registra leakage `PASS` e zero invocações de modelo.

## Protocolo congelado

- `llm-rca-pilot-v1`, OpenAI Responses API, `gpt-6-luna`.
- Reasoning `standard`, effort `medium`; service tier `default`; store/stream `false`; verbosity `low`; máximo 16.384 tokens.
- Temperature e top_p omitidos; sem ferramentas, web/file search, code interpreter, estado de conversa ou `previous_response_id`.
- Prompt `rca-prompt-v1`, SHA-256 `2e107e3c7f45b843bdad435f8c233dfe5085f3589c56ddd2bf04da12998fa86b`.
- Schema `rca-schema-v1`, Structured Outputs `rca_diagnosis`, strict, SHA-256 canônico `00f6a92d2946e1e9d2482eb75e69e7244763562b422d73f35b3cb6b6a3c540c7`.
- Plano privado de execução: ordem determinística embaralhada com seed `1301`; IDs opacos, paths e tratamentos ficam fora do model request.

## Controles implementados

- Request builder puro: lê/verifica prompt/schema hash e recebe somente o conteúdo RCA Input; não importa SDK, não usa key e não faz rede.
- Validador local: exatamente três ranks `[1,2,3]`, confidence limitada e não crescente, fault/location vocabulary, regras de nullability, hipóteses duplicadas e evidence IDs existentes no input. Respostas inválidas são rejeitadas sem reparo.
- Testes cobrem os quatro tipos de localização, schema estrito, rank/confidence, IDs conhecidos/desconhecidos/vazios, duplicidade, hashes, os dez inputs e ausência de metadata wrapper/path no request.
- Leakage do dataset permanece respaldado pelo pacote de auditoria oficial existente; Ground Truth não é lido pelo módulo de request.

## Interpretação

O conjunto é um **pilot / pipeline validation**, não um experimento estatístico definitivo. Seu objetivo posterior é validar runner, prompt, schema, parser, evaluator, métricas, auditabilidade, leakage e contabilização de custo antes de ampliar execuções. Mudanças após observar outputs exigem nova versão de protocolo; resultados de versões diferentes não serão combinados silenciosamente.

Estado ao final desta etapa:

```text
dataset: FROZEN
prompt/schema/model config: FROZEN
RCA Inputs: 10
LLM calls: 0
RCA runs: 0
```
