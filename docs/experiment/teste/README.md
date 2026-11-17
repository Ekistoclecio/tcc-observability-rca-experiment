# Pacote de auditoria independente — experiment-v2

Este pacote contém metadados de execução, hashes, amostras nativas e cadeias representativas F4/F5. O RAW completo permanece em `docs/experiment/results/`; as amostras apontam para seus arquivos de origem. Ground Truth aparece somente nos arquivos de auditoria de fault e não foi incluído nos RCA Inputs.

`hashes.sha256` cobre todos os arquivos do pacote, exceto ele próprio. O manifest explica que os hashes dos bundles RAW completos estão nos próprios bundles e são referenciados em `raw-summary.json`. A integridade dos resultados é resumida em `validation/artifact-integrity.json`.
