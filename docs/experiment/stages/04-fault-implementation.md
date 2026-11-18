# Stage 04 — faults F1–F5 e recuperação

## Objetivo

Aplicar uma fault controlada por vez, confirmá-la pelo mecanismo de controle, registrar Ground Truth, validar recuperação e remover recursos temporários.

## Mecanismos

`scripts/experiment/fault_smoke.py` coordena preflight, injeção, probes funcionais, Ground Truth e cleanup. F1 é StressChaos em `checkoutservice`; F2 é NetworkChaos na aresta `checkoutservice → paymentservice`; F3 escala `paymentservice` a zero; F4 ativa uma flag de exceção em `paymentservice/Charge`; F5 escala `redis-cart` a zero.

Confirmação é específica ao mecanismo: seleção/injeção Chaos Mesh, réplicas/Pods/endpoints ausentes ou flag presente no Pod correto. Recuperação verifica restauração do control plane e fluxo normal; readiness isolada não prova que canais gRPC persistentes reconectaram. Recursos Chaos, flags e escalas são limpos mesmo após erro.

## Correções consolidadas

- Para serviços escalados a zero, aguardar Pods e endpoints, não somente réplicas desejadas; Pods em terminação/canais já abertos podem produzir falso sucesso.
- Tratar listas vazias do Kubernetes explicitamente.
- Fazer probes limitados de recuperação até fluxo funcional, com resultados control-side.
- F5 verifica impacto nas rotas que realmente consultam carrinho, pois chamadas normais à loja podem depender de Redis.
- F1/F2 não precisam gerar HTTP failure: confirmação da fault é control-plane, e a consequência é analisada depois na telemetria.

## Invariantes

Somente uma fault ativa; Ground Truth nunca usa telemetria; nenhuma próxima fault começa antes de validação, recuperação e cleanup do caso atual. Estado: mecanismos e controle F1–F5 implementados.
