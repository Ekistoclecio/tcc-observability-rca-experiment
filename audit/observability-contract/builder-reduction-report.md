# Dataset Builder reduction report — Common Observability Contract v1

Before/after bytes below are recomputed from the same immutable RAW files for each new smoke run. “Before” applies the prior native-preserving normalizer shape; “after” applies Contract v1. This is an engineering size comparison, not a diagnostic-quality result. The old trace view also inherited the process-ID merge defect documented in `after-parity-report.md`.

| Fault | Scenario | Before bytes | After bytes | Reduction | Before metrics/logs/traces/spans | After metrics/logs/traces/spans |
|---|---|---:|---:|---:|---|---|
| F1 | A | 4,065,601 | 2,218,147 | 45.44% | 870/1933/114/2798 | 97/1933/110/2794 |
| F1 | B | 12,657,982 | 3,029,212 | 76.07% | 6246/1928/110/2794 | 97/1928/110/2794 |
| F4 | A | 4,053,932 | 2,185,549 | 46.09% | 909/1951/116/2712 | 107/1951/111/2707 |
| F4 | B | 12,576,301 | 2,958,270 | 76.48% | 6254/1890/111/2707 | 107/1890/111/2707 |
| F5 | A | 3,708,903 | 1,953,424 | 47.33% | 992/1743/102/2380 | 117/1743/96/2374 |
| F5 | B | 12,367,003 | 2,623,305 | 78.79% | 6879/1668/96/2374 | 117/1668/96/2374 |

Canonical RCA Request bytes (post-contract inputs):
- F1 A: 2,369,764 bytes; metrics/logs/traces/spans=97/1933/110/2794.
- F1 B: 3,213,275 bytes; metrics/logs/traces/spans=97/1928/110/2794.
- F4 A: 2,335,085 bytes; metrics/logs/traces/spans=107/1951/111/2707.
- F4 B: 3,138,350 bytes; metrics/logs/traces/spans=107/1890/111/2707.
- F5 A: 2,086,981 bytes; metrics/logs/traces/spans=117/1743/96/2374.
- F5 B: 2,783,114 bytes; metrics/logs/traces/spans=117/1668/96/2374.
