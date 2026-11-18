# Stage 10 — Dataset Builder

## Objetivo

Projetar RAW validado em datasets comparáveis para RCA, com regras determinísticas e proveniência reversível.

## Implementação

O Builder em `scripts/experiment/` consome somente bundles RAW aprovados e produz Dataset A/B separado sob contrato comum. Seletores, ordenação, disponibilidade e classificação são explícitos; cada evidência rastreia sua fonte RAW. O Builder não lê Ground Truth para selecionar/ordenar evidência nem usa faults esperadas para preencher sinais.

Métricas preservam unidade/intervalo/serviço; logs preservam mensagem/severidade/contexto natural; traces preservam serviço/operação/kind/duração/resultado/IDs. Representações duplicadas de CPU não são somadas como se fossem sinais independentes. Ausência permanece declarada.

## Proveniência, validação e estado

Manifest de build registra inputs/hashes, regras/versão, outputs e mapping de evidência. Testes cobrem determinismo, schema, coverage, leakage, hashes e projeção source→evidence. Fixtures sintéticas permanecem nos testes; outputs antigos foram removidos. Estado: pronto para RAW de nova execução.
