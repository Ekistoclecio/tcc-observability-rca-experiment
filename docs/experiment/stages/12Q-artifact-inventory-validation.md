# Etapa 12Q — inventário externo de artifacts

## Correção

O inventário externo agora usa uma única enumeração de caminhos em `external_inventory_paths(root)`. Ela exclui somente `root/hashes.sha256`, o manifesto que está sendo gerado/verificado, e inclui todos os demais arquivos regulares, inclusive manifests `hashes.sha256` aninhados em bundles RAW.

`hash_inventory()`, `write_hashes()` e `verify_hash_manifest()` compartilham essa enumeração. O publicador de resultados também gera sua hash list externa por `write_hashes()`, mantendo a mesma regra no staging de `results/` e no pacote `teste/`. Os bytes dos bundles e manifests internos não são alterados.

## Regressões requeridas

Os testes verificam exclusão do manifesto raiz, inclusão ordenada de três manifests aninhados, hash SHA-256 dos bytes exatos, falha quando um manifest interno é alterado e publicação final com manifest interno protegido pelo inventário externo. A bateria que revelou o problema permanece invalidada e serve apenas como evidência diagnóstica; qualquer bateria oficial deve reiniciar em F1 após novo commit e preflight.

## Validação pré-commit

As suítes completas passaram: 76 testes em `scripts/experiment` e 32 em `tests/experiment`. A cópia temporária do staging da bateria invalidada incluiu e verificou os seis manifests aninhados omitidos anteriormente. Um dry-run sem faults coletou métricas, logs A/B, traces A/B e control: 46 arquivos RAW verificados, boundaries Loki com delta de `0 ns`, 18 checks semânticos normais, Builder PASS e inventário final temporário PASS. `control/workload.json` permaneceu no staging. Esse dry-run não é resultado experimental nem foi publicado.
