# BLOCKED — Etapa 12: empacotamento do RAW global

## Estado

- Branch: `experiment-v2`.
- Commit testado: `a51a09eae1c7b5cf8216ab96c4a6b15a1ce05dcd`.
- A bateria oficial iniciou em F1 e executou F1–F5 nessa ordem.
- Todos os cinco `fault-smoke.json` indicam `PASS`; os Ground Truths estão `RECOVERED` e elegíveis; cada fault possui RAW, Dataset e RCA Input no staging temporário.
- A validação semântica F4/F5 passou. A coleta global RAW e a verificação dos 46 arquivos na origem também passaram.
- Nenhum resultado final foi publicado em `docs/experiment/results/`; o pacote de auditoria não foi finalizado. A bateria está **INVALIDADA** para reporte final.
- LLM não foi iniciado.

## Observado e esperado

O runner parou em `global RAW hash mismatch: control/workload.json` durante a montagem dos resultados. O manifesto e o hash list da coleta global incluem `control/workload.json`; o diretório RAW de origem contém esse arquivo com 9.953 bytes e SHA-256 `370a8c64a6259e0fd15c19642e59f3690cf4f86fa7c6ad24f9aeae61e219325e`.

A primeira divergência ocorre na cópia do RAW para staging: `results/global-raw-manifest.json` e `results/global-raw-hashes.sha256` foram copiados, assim como `datasetA/` e `datasetB/`, mas `results/control/workload.json` não existe. O verificador então tenta validar uma entrada listada cujo arquivo não foi copiado.

## Causa raiz — PROVEN

Em [`run_integrated_validation.py`](../../../scripts/experiment/run_integrated_validation.py), a finalização copia os diretórios de sinais A/B e os arquivos de manifesto/hash, mas omite `global-raw/control/` antes de chamar `verify_global_raw_hashes(staging)`. A coleta RAW original está íntegra; a perda ocorre no empacotamento dos resultados.

Evidência compacta preservada em [`BLOCKED-12-artifacts-1794876161/`](BLOCKED-12-artifacts-1794876161/):

- `global-raw/control/workload.json` existe e o SHA corresponde ao manifesto e ao hash list;
- `results/global-raw-hashes.sha256` lista o mesmo SHA para `control/workload.json`;
- `results/control/workload.json` está ausente;
- os cinco diretórios F1–F5 contêm fault smoke PASS, Ground Truth elegível/recuperado, RAW íntegro, Dataset e RCA Inputs;
- `results/semantic-validation.json` indica PASS.

O diretório preserva os manifests/hash lists originais e o artifact de controle. O resumo registra explicitamente que essa evidência vem de uma bateria invalidada e não pode ser publicada como resultado final. O staging completo e o restante da coleta continuam na área temporária da execução, fora de `docs/experiment/results/`.

## Invariante afetado

O pacote global RAW precisa preservar todos os artefatos nativos e de controle declarados pelo manifesto. O hash list não pode declarar um arquivo ausente no pacote final.

## Correção recomendada

Copiar `global-raw/control/` integralmente para `staging/control/`, sem alterar bytes ou remover sua entrada do hash list. Adicionar um teste focado que percorra todas as entradas do hash list e prove que cada arquivo está presente e tem o SHA indicado.

A correção não muda fault, Ground Truth, instrumentação, Builder ou tratamento A/B. Ainda assim, como o runner final foi alterado após uma bateria física, a próxima execução oficial deve usar um novo commit congelado e reiniciar desde F1; os F1–F5 desta bateria não podem ser combinados com uma futura execução.

## Decisão nesta etapa

## Resolução 12D — correção local validada

O empacotador agora copia `datasetA/`, `datasetB/` e todo `control/` recursivamente para o staging, preservando os bytes. Antes da publicação, o verificador confere cada entrada do hash list, rejeita caminho ausente, arquivo não regular ou hash divergente, e confere também os caminhos, tamanhos e hashes declarados pelo manifesto. A publicação final valida uma cópia temporária ao lado de `results/` e só então renomeia o diretório para o destino.

Testes do runner: **18/18 PASS**. Testes do coletor RAW: **6/6 PASS**. Uma regressão contra a coleta global preservada da bateria invalidada verificou **46/46** arquivos e `control/workload.json` byte-for-byte. Essa coleta foi usada somente como fixture diagnóstica; não foi promovida a resultado. `git diff --check`, compilação Python e validação JSON passaram.

A bateria anterior continua **INVALIDADA**. A correção de packaging passou os gates locais, mas ainda requer novo commit congelado, provenance, preflight e bateria física completa desde F1. Nenhum resultado dessa bateria anterior pode ser publicado.
