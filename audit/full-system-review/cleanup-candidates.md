# Candidatos de limpeza

Nenhuma remoção executada.

## REMOVE

- Relatórios de cobertura redundantes/contraditórios depois de criar ledger único. Risco baixo após migração.
- Classificação de stdout legado sem uso no caminho shared, após confirmar referências.
- Aliases de outcome sem uso nos formatos RAW retidos, depois de inventário.

## SIMPLIFY

- contract.py mistura parse, temporalidade e política: separar em etapas.
- allowlists repetidas entre Dataset Builder, RCA projection e auditoria: uma policy versionada e checagem independente.
- cobertura textual: separar declaração estática, build, runtime e backend.
- runner deve agregar gates; source FAIL não pode coexistir com overall PASS.
- callbacks precisam declarar unidade request/attempt/retry.

## CONSOLIDATE

- Um ledger de serviço/fault, um canonicalizer por conceito, um relatório por request contendo provenance e leakage.
- Relatórios de source events e log parity podem compartilhar identidade factual do contrato; RAW inventory fica independente.

## KEEP

- backends isolados, RAW completo, Ground Truth do control plane, janela e counters explícitos, missing_baseline distinto de zero, treatment A custom/B W3C, leakage, schema e projection provenance independente.

Não recomendo rollback amplo. Se proveniência e builds revelarem shared callbacks frágeis, reduzir ao conjunto mínimo F1–F5 mantendo braços experimentais distintos. Não preservar sunk cost por volume de código.

