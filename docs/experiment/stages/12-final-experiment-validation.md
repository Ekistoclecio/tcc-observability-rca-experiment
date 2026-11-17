# Etapa 12 — validação integrada F1–F5

**SUCCESS.** Bateria executada no commit `35069e451c95969962753f2ed1370fb4c77d4c0a`; nenhuma chamada de LLM foi feita.

- Resultados completos e RAW: [`results/`](../results/).
- Pacote independente: [`teste/`](../teste/), 1780782 bytes, hashes verificados.
- F1–F5: fault confirmada pelo control plane, recuperação/cleanup, RAW, Dataset e RCA Input validados.
- A/B observaram a mesma execução; os sinais nativos foram preservados, sem equalização post-hoc.
- Ground Truth ficou fora dos RCA Inputs; proveniência e hashes foram verificados.
- O histórico em `reports/` conserva blockers/incidentes reais das etapas de desenvolvimento.
