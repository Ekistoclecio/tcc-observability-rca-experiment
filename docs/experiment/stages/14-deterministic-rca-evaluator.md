# Stage 14 — evaluator determinístico RCA

## Objetivo

Pontuar predictions congeladas somente após a inferência, associando-as a Ground Truth independente e mantendo uma fronteira clara entre runner model-visible e evaluator.

## Implementação

`scripts/experiment/evaluation/evaluator.py` não contém client nem chamada de rede. Lê exclusivamente os arquivos autocontidos de `results/run-NNN/`: manifest, `ground-truth.json` e `llm/responses/`. A avaliação ocorre depois que predictions foram congeladas. Ground Truth é lida nesta etapa, nunca pelo runner LLM. Um único `evaluation/results.json` contém `cases` (fault→repetition→treatment), `faults` e `overall`; o inventário externo da run cobre também esse resultado.

## Métricas diagnósticas

- **Location exact match:** igualdade de kind e componentes canônicos. `service_edge` preserva direção; `operation` exige serviço e operação exatos; service não equivale a operation; datastore é exato. Sem fuzzy match, aliases ou normalização pós-hoc.
- **Fault type exact match:** igualdade exata do enum.
- **Diagnosis correct:** location e fault type corretos simultaneamente.
- **Accuracy@1:** hipótese rank 1 completamente correta.
- **Accuracy@3:** alguma hipótese entre ranks 1–3 completamente correta.
- **MRR:** `1/rank` da primeira hipótese completamente correta; zero se ausente.
- **Location Accuracy** e **Fault Type Accuracy:** medidas separadas no rank 1.

## Métricas operacionais

`input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`, `estimated_cost_usd` e `latency_ms` vêm dos artifacts efetivamente registrados na resposta do runner. Tokenização não é recalculada; reasoning não é somado novamente ao total; custo não é recalculado com uma tabela posterior. A agregação informa totais/médias relevantes por tratamento, além de mean/median/min/max de latência. Deltas são sempre `B - A`; sinal positivo é apenas valor maior, não juízo automático de qualidade.

A unidade pareada é a mesma instância física (fault e repetition) observada por A e B. O evaluator consome `fault_repetitions` do manifest, aceita subconjuntos e contagens heterogêneas e agrega somente instâncias executadas; com cinco repetições uniformes, agrega 25 pares físicos e 25 diagnósticos por tratamento. Respostas sem status `COMPLETED` e validação semântica PASS mantêm outcome operacional e usage, mas não recebem métricas diagnósticas numéricas. Não há crédito parcial para Accuracy@k.

## Limites metodológicos

O piloto de desenvolvimento não é evidência científica e seus valores foram removidos. A implementação não executa teste de significância, não gera alegação generalizável e não incorpora LLM-as-a-Judge, qualidade de explicação, Evidence Precision ou Hallucination Rate. Qualquer extensão dessas métricas requer protocolo separado.

## Validação e estado

Testes sintéticos cobrem cada localização, ranks, top-k, reciprocal rank, métricas separadas, custo/tokens, agregação e deltas, N=1, N=5 e contagens mistas/subconjuntos. O evaluator exige A/B e Ground Truth elegível para cada instância configurada, mas não exige faults desabilitadas; não lê arquivos de Ground Truth fora da run e não faz chamadas de rede. Estado: código e regras estão prontos para avaliar uma nova execução; não há evaluation artifacts armazenados.
