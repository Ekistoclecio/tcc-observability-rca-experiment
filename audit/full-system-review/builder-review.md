# Revisão do Dataset Builder

scripts/experiment/dataset/contract.py (cerca de 900 linhas) combina parsing Prometheus/Loki/Jaeger, mappings semânticos, séries, counters, histogramas, gauges, logs e spans. scripts/experiment/rca/builder.py aplica projeção e monta requests. projection_provenance.py é separado do sanitizer de produção e usa model-visible-projection-v1.json.

| Transformação | Classe | Avaliação |
|---|---|---|
| nomes nativos→conceitos | ESSENTIAL_NORMALIZATION | manter mapping explícito |
| janela RCA | EXPERIMENTAL_TREATMENT | manter, GT só timestamps/elegibilidade |
| baseline/delta/reset | ESSENTIAL_NORMALIZATION | manter; missing != zero |
| histogram compact | LEGITIMATE_COMPACTION | manter se derivado de amostras válidas |
| gauge summary | LEGITIMATE_COMPACTION | validar que preserva CPU |
| omissão delta zero | LEGITIMATE_COMPACTION | simétrica, apenas cumulativos |
| gate shared marker | EXPERIMENTAL_TREATMENT | legítimo, presença ausente deve bloquear |
| error/span outcome | ESSENTIAL_NORMALIZATION | manter, sem esconder mismatch de origem |
| remoção metadata | LEGITIMATE_COMPACTION | allowlist estreita |
| heurísticas de texto log | WORKAROUND/OBSOLETE | RAW-only após shared event gating |
| projection provenance | ESSENTIAL_VALIDATION | útil, mas prova transformação, não metodologia |

A validação de projeção não chama o sanitizer de produção, portanto não é cegamente circular. Limites: policy pode estar duplicada/correlacionada; diff posicional depende de ordem; conjunto de evidence IDs não detecta duplicidade. Recomenda-se separar parsing backend, semântica, temporalização e serializer, depois de estabilizar a origem. Hoje, Builder pode converter payload sem marker em ausência silenciosa de logs.

