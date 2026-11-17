# Etapa 12T — validação do cleanup temporário da finalização

## Causa e correção

A reprodução controlada confirmou que `shutil.rmtree()` falhava ao remover arquivos RAW dentro de diretórios temporários somente leitura (`0555`), retornando `EACCES` para `hashes.sha256`. A falha pertence ao cleanup do workspace descartável; não indica corrupção ou falha de leitura do pacote publicado.

`remove_temporary_tree()` agora prepara permissões de escrita/travessia somente nos diretórios da árvore temporária descartável e então executa uma única remoção. Não há retry, `chown`, alteração de bytes de arquivos nem normalização de modos dos artifacts publicados. Se a remoção falhar, registra traceback e contexto de filesystem e relança a exceção.

A mesma função é usada para o cleanup de workspaces, scratch final e staging temporário. A publicação de `results/` e `teste/` é verificada antes da emissão do stage `SUCCESS`; o JSON `SUCCESS` é a última publicação filesystem da finalização.

## Validação pré-bateria

- Teste de regressão pelo caminho real `PreserveWorkOnError.__exit__`, com diretórios RAW aninhados em `0555`, manifest e chunk em `0444`: PASS.
- Bytes e modos dos arquivos no instante anterior à remoção: preservados; somente diretórios temporários foram preparados para remoção.
- Falha simulada de `rmtree`: exceção original relançada, uma chamada, sem retry; diagnóstico com traceback/contexto: PASS.
- Smoke de finalização sobre cópia independente do artifact preservado: `finalize_results → create_audit_package → PreserveWorkOnError cleanup → publish_finalization → verificações finais` PASS.
- Integridade do smoke: 1081 hashes em `results`, 27 em `teste`; origem preservada com bytes e modos idênticos; stage SUCCESS publicado por último.
- Testes Python: `scripts/experiment` 81 PASS; `tests/experiment` 32 PASS.
- `py_compile` e `git diff --check`: PASS.
- Stress 100 runs: não é gate desta etapa. A tentativa diagnóstica anterior parou porque o harness não configurou `KIND_BIN`; não foi evidência contra o cleanup.

Nenhum fault foi executado e nenhuma bateria oficial foi iniciada nesta validação pré-bateria. As baterias anteriores permanecem invalidadas.
