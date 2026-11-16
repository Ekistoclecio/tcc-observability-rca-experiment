# Arquitetura alvo mínima recomendada

## Invariante

~~~text
uma execução física
  → uma boundary semântica por operação
  → adapters independentes A/B
  → mesmos fatos diagnósticos
  → formas/correlação distintas
~~~

## Fluxo recomendado

1. **Fonte da verdade do deploy:** commit imutável → build da imagem com OCI source revision → digest de plataforma → imagem carregada no Kind → Deployment referenciando digest → pod imageID validado antes do workload.
2. **Boundaries por linguagem:** uma função/interceptor local mede a operação uma vez e entrega (service, operation, kind, target, duration, outcome) aos adapters disponíveis. Não criar framework cross-language.
3. **A:** adapter Prometheus convencional, logger tradicional para eventos explícitos do contrato, tracing custom e headers próprios.
4. **B:** adapter OpenTelemetry, logs estruturados por API OTel, propagação W3C e correlação nativa; spans nas mesmas boundaries lógicas, sem duplicar spans automáticos.
5. **Eventos:** emitir apenas eventos de aplicação definidos no contrato, com event_name estável. A serializa a estrutura no stdout; B usa atributos/correlaciona com span. Marcador técnico somente no transporte/audit.
6. **RAW:** preservar snapshots integrais e imutáveis; separar sinal nativo/raw de visão model-visible.
7. **Builder:** coletar janela, mapear conceitos nativos, calcular semântica temporal com bounds reais, compactar histogram/gauge e retirar metadata definida por contrato. Builder não escolhe fatos para igualar A/B.
8. **RCA Input:** uma projeção única, schema/prompt versionados, sem identidade operacional, Ground Truth, cenário ou IDs da run.
9. **Validação:** seis gates pequenos e independentes:
   - deployment provenance;
   - Ground Truth/mecanismo;
   - observações compartilhadas de origem;
   - Common RCA Dataset;
   - Dataset→RCA projection/leakage;
   - integridade/publicação de artifacts.

## Reduções arquiteturais

- Manter um contrato de dados versionado; documentação e matriz por serviço devem ser geradas ou validadas contra ele, sem PASS por substring isolada.
- Separar “compatibilidade de RAW histórico” da validação de runs atuais. Não manter caminhos de compatibilidade ativos quando não há necessidade de reprocessar o histórico.
- Remover heurísticas para converter stdout genérico em evidência; selecionar explicitamente a origem/shape do contrato.
- Unificar gate de execução: o orquestrador deve parar se imagem, source events ou artifacts mandatórios não estiverem provados.
- Não exigir igualdade numérica RAW. Exigir mesmas observações semânticas e diferenças de coleta dentro de um atraso documentado.

