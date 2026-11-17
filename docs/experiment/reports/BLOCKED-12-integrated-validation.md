# BLOCKED — Etapa 12 integrated validation

- Último commit presente: `a51a09eae1c7b5cf8216ab96c4a6b15a1ce05dcd` (`experiment-v2`).
- Observado: `global RAW hash mismatch: control/workload.json`
- Esperado: normal workload, F1-F5, RAW, Dataset, RCA Input, semantic coverage and artifact integrity all PASS.
- Primeira divergência: `GLOBAL_RAW_RESULT_PACKAGING`; o empacotamento copiou datasets, manifesto e hash list, mas omitiu `global-raw/control/`.
- Root cause: PROVEN — o arquivo `control/workload.json` está declarado no manifesto/hash list e existe na origem, mas não foi copiado para o staging.
- Evidência compacta preservada: `docs/experiment/reports/BLOCKED-12-artifacts-1794876161/`.
- Diagnóstico detalhado e resolução: `docs/experiment/reports/BLOCKED-12-global-raw-packaging.md`.
- Relatório estruturado: `docs/experiment/reports/BLOCKED-12-integrated-validation.json`.
- A correção local do empacotador e os testes de regressão passaram; a bateria integrada permanece INVALIDATED até ser repetida integralmente desde F1 com um commit congelado novo.
- Nenhum resultado final foi publicado e nenhuma chamada de LLM foi executada.
