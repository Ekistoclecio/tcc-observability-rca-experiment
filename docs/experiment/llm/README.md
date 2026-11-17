# Protocolo LLM/RCA — piloto v1

**This protocol is frozen before any LLM invocation.** Nenhuma chamada foi feita ao congelar estes artifacts.

## Congelamento

- Protocolo: `llm-rca-pilot-v1`.
- Prompt runtime: `rca-prompt-v1.txt`; o SHA-256 no `protocol-v1.json` cobre os bytes exatos do arquivo.
- Structured Output: `rca-schema-v1.json`; seu SHA-256 é calculado sobre JSON canônico UTF-8 (chaves ordenadas, separadores compactos, sem ASCII escaping).
- Os dez RCA Inputs são imutáveis; `rca-inputs.sha256` lista os SHA-256 dos bytes originais. Não há cópias ou reformatações dos RCA Inputs neste diretório.
- `blind-execution-plan-v1.json` é metadata privada para o futuro runner/evaluator. Os IDs opacos, caminhos e tratamentos nunca entram no request model-visible.

Qualquer alteração posterior de prompt, schema, modelo ou configuração após observar outputs exige uma nova versão de protocolo. Outputs de versões diferentes não podem ser misturados silenciosamente.

## Request e validação local

`scripts/experiment/llm/protocol.py` carrega os artifacts congelados, verifica seus hashes, confere os hashes dos RCA Inputs e constrói um valor de request Responses API sem criar client nem acessar rede. O request contém apenas as instruções congeladas e o conteúdo JSON do RCA Input. Não carrega Ground Truth nem recebe caminho, nome de arquivo, fault ID ou tratamento.

`response_validator.py` valida a estrutura estrita e as regras semânticas locais: exatamente três ranks em ordem, confidence não crescente, taxonomias permitidas, nullability por tipo de localização, ausência de diagnóstico duplicado e referências apenas a evidence IDs do input. A validação nunca corrige ou reordena a resposta.

## Interpretação do piloto

O dataset contém cinco instâncias físicas de fault, uma execução física por fault e dois tratamentos de telemetria, totalizando dez diagnósticos LLM futuros. Este piloto valida runner, prompt, schema, parser, evaluator, métricas, auditabilidade, proteção contra leakage e contabilização de custo. Ele não sustenta, isoladamente, conclusões estatísticas fortes ou generalizáveis.

A ordem futura está pré-fixada por seed no plano privado. Ground Truth permanece separado para o evaluator independente e não é carregado pela construção de requests.

## API

A estrutura local segue a Responses API: `reasoning.mode`/`effort`, `service_tier`, `store`, `stream`, `max_output_tokens` e `text.format` com `json_schema` estrito. Temperature e top_p são omitidos. Nenhum SDK ou API key é necessário para esta etapa.

## Estado

```text
Dataset: FROZEN
Prompt/schema/config: FROZEN
RCA Inputs: 10
LLM calls: 0
RCA runs: 0
```

## Etapa 13B — execução do piloto

As dependências do executor estão fixadas em `scripts/experiment/llm/requirements.txt`.
Instale-as em um ambiente virtual antes de executar testes ou o runner. O comando
`freeze-plan` gera `execution-plan-v1.json`; sua ordem usa somente os hashes dos
RCA Inputs e seus IDs opacos não entram nos requests.

O runner carrega explicitamente `<raiz-do-repositório>/.env` com
`python-dotenv` e `override=False`. `.env` precisa estar ignorado e não rastreado
pelo Git. `preflight`, `freeze-plan` e testes são offline. Os únicos comandos
que usam rede são `control-smoke` (um RCA Input sintético, fora do piloto) e
`run` (os dez Inputs congelados). O runner verifica hashes antes de cada caso,
usa o SDK oficial OpenAI com retries internos desligados e controla até três
tentativas transitórias por chamada lógica.

Faça commit do runner e deixe a árvore limpa antes da execução real. Em seguida,
rode `python -m scripts.experiment.llm.runner control-smoke`; somente se o smoke
terminar com status `COMPLETED`, rode
`python -m scripts.experiment.llm.runner run`. Os outputs ficam em `pilot-v1/`
e o smoke fica separado em `control-smoke/`. Não rode evaluator nem carregue
Ground Truth nesta etapa.

`pricing-v1.json` registra o snapshot do gpt-6-luna para tier padrão e inclui o
preço documentado de cache write. O custo fica `null` quando os detalhes de
usage ou do tier efetivo não permitem cálculo correto.
