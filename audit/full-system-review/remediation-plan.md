# Plano de remediação recomendado (não executado)

## Fase A — Proveniência de build/deploy
**Objetivo:** commit ligado a imagem e Pod. **Componentes:** image build, manifests, preflight. **Mudanças:** OCI revision, digest imutável, rollout de dez serviços, assertion por Pod. **Testes:** caso positivo e imagem stale negativa. **Saída:** revisão confirmada em cada Pod. **Impacto:** HIGH.

## Fase B — Cobertura executável
**Objetivo:** substituir status lexical por build/runtime. **Componentes:** matriz e serviços. **Mudanças:** N/A justificado e prova de build/execução. **Testes:** builds das linguagens e check de emissão. **Saída:** dez serviços com evidência. **Impacto:** HIGH.

## Fase C — Pipeline shared event
**Objetivo:** provar evento da aplicação até RAW em A/B. **Componentes:** adapters, serialização, Promtail/Collector/Loki. **Teste:** evento conhecido com event_name/campos corretos e treatment preservado. **Saída:** source parity real. **Impacto:** HIGH.

## Fase D — Unidade métrica e temporal
**Objetivo:** fixar logical request vs attempt/retry e cutoff. **Componentes:** completion callbacks, counters, exporters/Prometheus. **Testes:** lag, baseline, reset e convergência. **Saída:** contagem comparável sem usar traces. **Impacto:** HIGH.

## Fase E — Simplificação Builder
**Objetivo:** separar parse, semântica, temporal e projeção; falhar na ausência de evidência obrigatória. **Testes:** fixtures e determinismo. **Saída:** pipeline auditável. **Impacto:** MEDIUM/HIGH.

## Fase F — Consolidar gates
Deployment→GT→source parity→Dataset→provenance/leakage. Testes negativos para cada gate. Remover duplicidade sem retirar invariantes. **Impacto:** MEDIUM.

## Fase G — Fresh F4/F5
Só após A–F, commit limpo, build e digest confirmados. Uma execução de cada, congelada, sem retries silenciosos. **Impacto:** HIGH.

## Fase H — F1–F5
Só se G passar. Nova validation ID autocontida e artifacts de um commit/imagens verificadas. **Impacto:** HIGH.

A auditoria termina na recomendação; nenhuma fase foi executada.

