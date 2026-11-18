# Stage 09 — validação semântica RAW

## Objetivo

Validar cobertura e consistência do material RAW sem modificar conteúdo ou derivar Ground Truth de telemetria.

## Implementação

`scripts/experiment/validate_raw_semantics.py` aplica contratos por sinal: disponibilidade, tipos/atributos, timestamps, serviços/operações, correlação e fatos diagnósticos esperados. Compara semântica A/B no intervalo pareado; divergência de contagem bruta não é falha por si só. Campos indisponíveis permanecem desconhecidos, não preenchidos por inferência.

Boundary temporal é interpretado como RFC3339 e convertido para integer epoch nanoseconds. Igualdade é do instante absoluto exato, incluindo precisão de segundos a nanos e offsets equivalentes. Strings dos artifacts são preservadas; sem float, trim de zeros ou tolerância temporal. Instantes distintos falham com expected/actual e delta diagnosticáveis.

## Decisões, validação e invariantes

Representações textuais diferentes do mesmo instante passam. Não reescrever GT, manifest ou RAW. Fatos naturais A-only que não mudam cobertura semântica são `RAW_ONLY_DIFFERENCE`; não adicionar/remover eventos para equalizar contagens. Testes cobrem precisão, timezone, instante distinto e boundaries reais. Estado: validação semântica fail-closed.
