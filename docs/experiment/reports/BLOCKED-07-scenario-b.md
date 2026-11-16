# BLOCKED — Etapa 7: Scenario B OpenTelemetry

## Estado

- **Etapa:** 7 — Scenario B
- **Último commit bem-sucedido:** `fdbd151c4e47fc561cd87150ac9e666ef27a5f33` (`docs(experiment): record Scenario A runtime validation`)
- **Branch:** `experiment-v2`
- **Causa-raiz:** `PROVEN`
- **Nova execução física de fault necessária:** não; nenhuma fault foi executada nesta etapa. Uma nova validação normal será necessária após a correção.

## Observado

O stack B implantado contém Collector OTLP com pipelines somente de `traces` e `metrics`. Não há receiver/exporter nem pipeline `logs` no Collector B. O `promtail-b` descobre os mesmos Pods e lê os mesmos arquivos `/var/log/pods/.../*.log` que `promtail-a`; a diferença entre eles é o destino Loki.

O logger de `paymentservice` é Pino convencional e escreve em stdout. `PaymentService.Charge` chama esse logger durante a operação. Uma consulta direta aos dois Lokis encontrou o mesmo registro de `PaymentService#Charge invoked` nos dois cenários, com os mesmos campos de stdout e sem `trace_id` ou `span_id`. Os backends confirmam recepção da linha, mas não contextualização OpenTelemetry em B.

Também foram inspecionados o estado de runtime e as alterações locais. Os Pods estavam Ready e os cinco componentes do stack B estavam Ready. As imagens em execução antecedem as últimas alterações locais ainda não commitadas; portanto, os builds locais concluídos não provam comportamento runtime dessas alterações.

## Esperado

O design da experiment-v2 espera logs contextualizados no Scenario B quando há contexto ativo, com `trace_id` e `span_id` naturalmente disponíveis. O tratamento de A deve continuar com logs convencionais e correlação explícita limitada. A e B devem manter seus backends isolados.

## Primeira divergência

**Instrumentação de logging de B / saída de logs da aplicação.** A aplicação emite somente o registro convencional em stdout. Não há adaptação que exporte esse registro como log OTel contextualizado para o Collector B. A configuração do Collector não recebe logs. Assim, B volta a coletar o mesmo stdout bruto que A, antes de qualquer distinção de tratamento de logging.

## Evidências

1. `kustomize/experiment-v2/observability-b/collector.yaml`: define receiver OTLP e pipelines `traces` e `metrics`; não define pipeline `logs`.
2. `kustomize/experiment-v2/observability-b/loki-promtail.yaml`: `promtail-b` usa descoberta Kubernetes e o path `/var/log/pods/*$1/*.log`, enviando para `loki-b`.
3. `kustomize/experiment-v2/observability-a/loki-promtail.yaml`: `promtail-a` usa a mesma descoberta e o mesmo path, enviando para `loki-a`.
4. `src/paymentservice/logger.js`: Pino convencional com formatter de severidade; não injeta contexto OTel nem exporta logs OTLP.
5. `src/paymentservice/server.js`: `ChargeServiceHandler` escreve o registro de Charge pelo logger convencional.
6. Consultas diretas de `query_range` aos serviços `loki-a` e `loki-b` retornaram o mesmo registro de Charge, sem campos de correlação OTel. A consulta foi feita sem Dataset Builder.
7. `docs/experiment/experiment-design.md`, seções 9, 10 e 14: espera logs contextualizados e correlação explícita de logs com traces no Scenario B quando há contexto.

## Invariante afetado e impacto científico

- **Invariante:** B preserva correlação de logs com traces quando contexto está ativo, enquanto A permanece sem correlação explícita geral.
- **Impacto:** o tratamento de contextualização/correlação de logs ainda não está implementado. A separação de backends, por si só, não cria o tratamento experimental. Não é possível aprovar a cobertura de logging de B nem avançar para a coleta RAW.

## Por que não foi corrigido automaticamente

Uma alteração direta ao logger Pino que acrescente IDs ao stdout compartilhado também tornaria esses IDs visíveis para o Promtail A, mudando o tratamento de A. Remover os campos depois em um pipeline seria filtragem pós-emissão e reduziria a integridade do RAW. Uma saída B separada requer integração de logging contextual/OTel em múltiplas linguagens e uma rota OTLP Logs independente, além de validação de que cada implementação preserva o evento real sem duplicações. Isso é uma extensão arquitetural cross-language, não uma correção local de configuração comprovadamente segura.

## Correção recomendada

Adicionar ao Scenario B um caminho de logs contextualizados separado do stdout convencional de A: integração idiomática de logging OTel por linguagem, exportação OTLP Logs para o Collector B e pipeline de logs do Collector para Loki B. Manter o stdout convencional de A sem IDs de trace/span. Validar o mesmo evento natural nos backends A e B e provar que só B recebe correlação explícita. Não adicionar eventos artificiais nem filtrar logs para fabricar paridade.

## Alternativa

Revisar explicitamente o desenho metodológico para retirar ou restringir a correlação de logs como tratamento B. Essa alternativa conflita com o desenho atual e exige decisão explícita; não foi aplicada.

## Informação necessária para retomar

Confirmar que a solução aprovada é a integração idiomática de OTel Logs por linguagem com exportação B separada, preservando stdout A, ou decidir formalmente alterar o tratamento metodológico. Em qualquer alternativa, definir o comportamento para serviços sem evento natural durante workload normal e a política para evitar duplicação entre stdout e OTLP.

## Validações locais concluídas

- `git diff --check`: PASS.
- Builds de imagens executados para os serviços alterados em Go, Node.js, Python, .NET e Java: PASS, incluindo o build .NET no container; esses builds não foram implantados depois das últimas alterações.
- `node --check` e `python3 -m py_compile`: PASS.
- Testes unitários de `paymentservice`: 3/3 PASS.
- Verificação runtime de logs em Loki A/B: o mesmo registro convencional chegou aos dois backends; correlação B ausente.
- Não houve alteração de Builder, Common RCA View, Ground Truth, faults ou workload nesta verificação.

## Estado da árvore de trabalho

As alterações locais da Etapa 7 permanecem sem commit para inspeção. Este relatório e seu JSON são os únicos novos artefatos em `docs/experiment/reports/`. Nenhum fault foi executado.
