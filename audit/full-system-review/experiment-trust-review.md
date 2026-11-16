# Confiança nos experimentos anteriores

| Run | Geração | Proveniência imagem | GT | RAW | Builder | Confiança |
|---|---|---|---|---|---|---|
| results-nov-007-001..005 | antes do contrato final | commit sem revisão embutida | evidência disponível | útil para execução histórica | válido para código histórico | PARTIALLY_TRUSTED; não valida implementação atual |
| Common Observability validation | contrato intermediário | não ligada ao source | varia | útil como snapshot | histórico | INVALIDATED para estado atual |
| F4 metric investigation | Builder de counters daquela geração | não provada | mesma execução | amostras reais | diagnóstico útil | PARTIALLY_TRUSTED para lag |
| final integrated / teste commit 6f1cfae | Common View intermediária | não provada | 5/5, leakage zero | pacote antigo | overall FAIL/log parity falhou | INVALIDATED como sucesso |
| canonical log F4/F5 smokes | policy intermediária | sem prova completa | útil | útil na época | compatível com policy da época | PARTIALLY_TRUSTED |
| Shared Observation so-115b-001/002 | anterior a marker commit 4ee97c1a | não provada | útil | útil | gate final ainda não executado | PARTIALLY_TRUSTED, não source parity final |
| Fresh F4 final-shared-smoke-f4-001 | marker esperado em código posterior | config local=Pod; commit não provado | injection/recovery PASS | 5 eventos A/B | 0 logs e source gate FAIL | INVALIDATED como prova shared; parcial para fault/RAW |
| bateria atual F1–F5 | nenhuma | — | — | — | — | NOT_PROVEN |

O mismatch 509360/e28cbc era comparação entre OCI/index e config digest; local config coincide com Pod. Isso não vincula o config a um Git commit. A pasta teste atual tem 133 arquivos, 11.4 MiB e status FAIL, gerada em commit antigo. Não combinar execuções de diferentes commits numa validação só.

