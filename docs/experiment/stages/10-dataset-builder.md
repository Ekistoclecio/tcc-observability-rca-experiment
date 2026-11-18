# Stage 10 — Dataset Builder

## Objetivo

Projetar RAW validado em datasets comparáveis para RCA, com regras determinísticas e proveniência reversível.

## Implementação

O Builder em `scripts/experiment/` consome somente bundles RAW aprovados e produz Dataset A/B separado sob contrato comum. Seletores, ordenação, disponibilidade e classificação são explícitos; cada evidência rastreia sua fonte RAW. O Builder não lê Ground Truth para selecionar/ordenar evidência nem usa faults esperadas para preencher sinais.

Métricas preservam unidade/intervalo/serviço; logs preservam mensagem/severidade/contexto natural; traces preservam serviço/operação/kind/duração/resultado/IDs. Representações duplicadas de CPU não são somadas como se fossem sinais independentes. Ausência permanece declarada.

## Proveniência, validação e estado

Manifest temporário de build registra inputs/hashes, regras/versão, outputs e mapping de evidência. Na run publicada, a saída relevante do Builder é `telemetry/F{fault}-R{repetition}-{A|B}.json`; ela inclui proveniência compacta para a janela RAW, hashes do bundle e versão/hash do Builder. Cópias intermediárias, staging e manifests de processamento são removidos antes da publicação. Testes cobrem determinismo, schema, coverage, leakage, hashes e projeção source→evidence.

## Escala e invariantes

O Builder executa uma vez por tratamento e instância física. Para N repetições, publica 10N arquivos de telemetry, sem misturar A/B ou janelas de fault. `telemetry` é derivada de Dataset Builder output, não uma cópia de RAW. Ground Truth não é usada para selecionar ou ordenar evidências.

## Estado final

Implementação pronta para N configurável; artifacts de validação anteriores não são mantidos.
