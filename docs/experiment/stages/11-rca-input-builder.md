# Stage 11 — contrato e construção de RCA Input

## Objetivo

Gerar representação model-visible mínima a partir do Dataset, sem tratamento, Ground Truth, IDs de fault ou caminhos.

## Implementação

`scripts/experiment/build_rca_input.py` projeta cada Dataset para `{schema_version, evidence}` conforme `scripts/experiment/llm/resources/rca-input.schema.json`. Evidências recebem IDs verificáveis; request LLM usa conteúdo JSON do arquivo, nunca filename/path. Treatment e mapping permanecem control-side para pareamento/evaluator.

Evidence possui tipos fechados de métrica/log/trace e atributos permitidos. A projeção remove campos de controle e valida referências/formatos. Ground Truth não é importada pelo Builder. Schema é validado localmente.

## Validação e invariantes

Testes verificam schema, IDs únicos, projeção determinística, sinais válidos, leakage e proveniência Dataset→RCA Input. Para cada instância física, dois inputs são construídos a partir do Dataset A/B correspondente. O payload exato model-visible (configuração congelada do modelo, instruções, telemetria e schema Structured Output) é arquivado como `llm/inputs/Fx-Rnn-T.json` antes da chamada ao SDK; o runner verifica que o request arquivado serializa o mesmo objeto passado a `responses.create`. Esse artifact exclui paths, identidade do caso, tratamento, Ground Truth e segredo.

## Escala e invariantes

A quantidade de repetições é parâmetro da execução, não constante científica no Builder. Uma run com N repetições contém 5N identidades físicas e 10N RCA Inputs. Os nomes usam repetition com dois dígitos e são únicos por fault, repetition e tratamento. O request inclui o conteúdo congelado do RCA Input, nunca o filename local.

## Estado final

Builder e contrato suportam N positivo configurável. Inputs gerados em execuções anteriores não são mantidos.
