# Stage 02 — workload determinístico

## Objetivo

Submeter as mesmas operações de negócio em sequência e taxa pré-fixadas, sem expor metadados experimentais ao SUT.

## Implementação

`scripts/experiment/workload.py` executa no host, fora do SUT. Configuração normativa em `config/experiment/workload.json`. Cada fluxo usa cookie jar próprio e percorre `GET /`, produto, criação/leitura do carrinho e checkout. Warm-up, steady state e drain são representados separadamente. A taxa de chegada é fixa, concorrência e timeouts são limitados e todos os requests iniciados são drenados antes de encerrar.

O cliente envia somente payload e headers funcionais exigidos pela loja. Não inclui run/fault/scenario/experiment IDs. Identificadores de sessão são os cookies normais do frontend. O loadgenerator upstream não é usado como origem de evidência RCA.

## Validação e invariantes

Testar configuração JSON, CLI, sintaxe e fluxo funcional sem fault; validar que uma etapa HTTP malsucedida faz o comando falhar. Repetibilidade significa mesmo algoritmo/configuração, não igualdade de latência ou contagens. Estado: workload determinístico implementado e independente do SUT instrumentado.
