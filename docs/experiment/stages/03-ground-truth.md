# Etapa 3 — Ground Truth independente

## Estado e commit

- Branch: `experiment-v2`.
- Commit de entrada: `b9998e856bb21e68434dd81aa1a7330ff51accb8`.
- Commit correspondente: `feat(ground-truth): record independent fault ground truth` (este registro é incluído nesse commit).
- Nenhuma fault ou consulta a telemetria foi executada.

## Contrato implementado

`config/experiment/ground-truth.schema.json` define o registro versionado. `scripts/experiment/ground_truth.py` implementa serialização JSON atômica, validação estrutural e a máquina de estados usada posteriormente pelo control plane.

O registro contém:

```text
fault_id
fault_type
root_location { kind, value }
requested_start
confirmed_start
confirmed_end
injection_confirmation
recovery_confirmation
execution_eligible
status
ineligibility_reason
```

Taxonomia aceita:

```text
cpu_saturation
network_latency
service_unavailable
application_exception
datastore_unavailable
```

Tipos de localização aceitos:

```text
service
service_edge
operation
datastore
```

As confirmações registram instante, verificação do plano de controle e evidência textual correspondente. A API não contém integração com os backends nem recebe telemetria.

## Transições e elegibilidade

```text
PENDING --confirm_injection--> ACTIVE
ACTIVE --confirm_recovery--> RECOVERED (eligible)
PENDING/ACTIVE --mark_ineligible--> INELIGIBLE
```

`confirmed_start` não pode preceder `requested_start`; `confirmed_end` não pode preceder `confirmed_start`. Uma execução sem confirmação de injeção ou recuperação não se torna elegível. Transições fora de ordem e alterações de estado recuperado para inelegível são rejeitadas.

## Validação

```text
python3 -m unittest discover -s tests/experiment -v
```

Resultado: **6 testes PASS**. Cobrem gravação/leitura e round-trip, estrutura do schema, timestamp anterior ao pedido, recuperação sem injeção, recuperação temporalmente anterior e ausência de confirmação de injeção/recuperação. Também passaram `py_compile`, parsing JSON do schema e `git diff --check`.

Os fixtures usam timestamps fixos e evidências simuladas exclusivamente para validar o formato e as transições. Não representam faults executadas nem Ground Truth de uma execução experimental.

## Limitações conhecidas

- O módulo fornece o modelo e a persistência; o orquestrador de faults ainda será implementado na Etapa 4.
- A semântica de cada evidência de confirmação será ligada ao mecanismo de controle específico de cada fault na etapa de faults. É proibido usar sinais A/B para preencher essas confirmações.
- O schema é acompanhado por validação executável no modelo Python; não foi adicionada dependência externa de validador JSON Schema.

## Resultado

**PASS** — o formato e as transições registram Ground Truth de forma independente da telemetria, com elegibilidade bloqueada até a confirmação de recuperação.
