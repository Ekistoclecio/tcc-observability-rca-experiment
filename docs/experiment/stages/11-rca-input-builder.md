# Stage 11 — contrato e construção de RCA Input

## Objetivo

Gerar representação model-visible mínima a partir do Dataset, sem tratamento, Ground Truth, IDs de fault ou caminhos.

## Implementação

`scripts/experiment/build_rca_input.py` projeta cada Dataset para `{schema_version, evidence}` conforme `scripts/experiment/llm/resources/rca-input.schema.json`. Evidências recebem IDs verificáveis; request LLM usa conteúdo JSON do arquivo, nunca filename/path. Treatment e mapping permanecem control-side para pareamento/evaluator.

Evidence possui tipos fechados de métrica/log/trace e atributos permitidos. A projeção remove campos de controle e valida referências/formatos. Ground Truth não é importada pelo Builder. Schema é validado localmente.

## Validação e invariantes

Testes verificam schema, IDs únicos, projeção determinística, sinais válidos, leakage e proveniência Dataset→RCA Input. Nova execução gera dois inputs por fault físico. Estado: builder e contrato preservados; inputs anteriores removidos.
