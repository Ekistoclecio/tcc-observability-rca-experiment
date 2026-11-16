# Etapa 11 — contrato de entrada RCA

## Resultado

**PASS.** Foi definido um envelope model-visible comum e foram geradas entradas auditáveis para F4/F5 em A e B. Nenhum modelo foi chamado.

Base implementada: `432e71d4` (`feat(dataset): normalize telemetry for RCA`).

## Contrato

Schema: [`rca-input.schema.json`](../rca-input.schema.json).

```json
{
  "schema_version": "1.0",
  "evidence": [
    { "id": "ev_…", "signal": "…", "service": "…" }
  ]
}
```

`evidence` é uma projeção de identidade dos fatos do Dataset: nenhum fato é selecionado por fault, Ground Truth ou cenário; nenhum fato é adicionado, reconstruído ou equalizado. Os IDs permanecem estáveis para que hipóteses futuras possam citar evidências. IDs de trace/span de B continuam presentes quando já existiam no Dataset; A não recebe correlação artificial.

O construtor recebe somente um arquivo Dataset e produz o payload e um sidecar de proveniência separado:

```bash
python3 scripts/experiment/build_rca_input.py \
  --dataset <dataset-A-ou-B.json> \
  --output <rca-input.json> \
  --provenance <provenance.json>
```

O sidecar registra hashes do Dataset e do payload, além do ponteiro `/facts/<index>` e hash canônico de cada fato. Ele não é parte da entrada model-visible. O comando falha se encontrar campos/valores de controle experimental, campos fora do contrato, IDs inválidos/duplicados ou se os destinos já existirem.

O contrato futuro de resposta continua sendo exatamente três hipóteses ordenadas com `location`, `fault_type`, `confidence`, `evidence_ids` e `explanation`. Nenhuma resposta foi gerada nesta etapa.

## Artefatos e validação

Entradas e sidecars:

| Janela | Cenário | Evidências | Payload SHA-256 |
|---|---|---:|---|
| F4 | A | 304 | `7a166c600efba4f9031f7d34bc19a207c87bf0b218f8d8669dfe0033eb608c97` |
| F4 | B | 282 | `f6500ac9a3fdcf500ac5831be82e139b77623950cf5f887f7501fec716abd209` |
| F5 | A | 170 | `0ab42d356861ac35069005923ded47473c6f75d4889e0cadc588df04c51e2eeb` |
| F5 | B | 165 | `eb1180862fa44624591d1e7a58e0abcaa23691675123f3c03dbade60e564bd8b` |

Arquivos em [`11-rca-contract/`](11-rca-contract/); resumo estruturado em [`11-rca-contract-audit.json`](11-rca-contract/11-rca-contract-audit.json).

- Schema Draft 2020-12 validado com `jsonschema` 4.10.3 e `FormatChecker`.
- Auditoria independente confirmou que cada entrada é exatamente o envelope e a lista de fatos do Dataset correspondente; todos os IDs e hashes de fatos conferem com os ponteiros e hashes do sidecar.
- Os quatro payloads passaram pelo schema; não contêm Ground Truth, fault/run/scenario IDs, mecanismo de injeção nem rótulo A/B.
- Em F4, ambos preservam o mesmo log `Payment charge failed` com severidade ERROR. O payload A permanece sem trace/span IDs; no payload B, a correlação do log continua ligada ao span de erro Charge existente na mesma entrada.
- Testes relevantes: **32/32 PASS** na suíte Python; `git diff --check`: PASS.
- Não houve nova execução física, mudança de telemetria, nem invocação de modelo.

## Limites

O payload A ou B é selecionado pelo controlador externo que fará uma chamada futura. A identidade do cenário fica fora do JSON enviado ao modelo; o contrato não implementa ainda runner, prompt, resposta, avaliação ou retries.

**Gate da Etapa 11: PASS.** Próxima etapa: preparar e validar a integração do pipeline, sem antecipar execução de LLM.
