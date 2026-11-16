# Auditoria técnica completa

## Escopo

HEAD d3670c83cf4f49764099e6f251bc238011595b62, main. Auditoria somente leitura; sem fault/Chaos/LLM e sem edição de implementação. Foram inspecionados Git, código, manifests, scripts, artifacts, pacote teste, cluster e backends read-only.

## Desenho científico reconstruído

Variável independente: organização, contextualização e correlação da telemetria. A usa mecanismos convencionais e correlação limitada; B usa OpenTelemetry, W3C e correlação estruturada. Mesmo SUT/workload e faults F1–F5; GT vem do control plane. Fluxo: RAW integral → dataset por janela → RCA Input; avaliação de IA fica fora desta etapa. A hipótese continua plausível se fatos diagnósticos forem equivalentes. Fresh F4 não demonstrou isso para o código alegado.

## Avaliação

- **Conceito:** saudável com condição; a hipótese pode ser isolada.
- **Implementação:** inconsistente e excessivamente complexa nas políticas/validators, não necessariamente irrecuperável.
- **Blocker principal:** source commit não está ligado verificavelmente à imagem/Pod; adicionalmente source parity FAIL não bloqueou overall PASS.
- **A:** convencional/custom ainda, mas não comprovado que a imagem executava a versão mais recente.
- **B:** OTel/W3C preservado; risco de auto/manual duplicate e families extras RAW.
- **Shared contract:** parcial como evidência; cobertura estática não prova runtime.
- **Builder:** útil, mas grande e pode produzir silêncio quando marker falta.
- **Validators:** sobrepostos; provenance não é totalmente circular, mas não prova verdade da policy.
- **GT:** control-plane independente e validado nas runs consultadas.
- **RCA Input:** contrato/provenance/leakage estão presentes; conteúdo Fresh F4 vazio para logs não foi tratado como falha geral.

## Top riscos
1. DEP-001: código executado não ligado ao commit.
2. VAL-001: source parity falhou sem bloquear.
3. OBS-002: shared events ausentes do Dataset.
4. MET-001: counters divergem no cutoff; lag confirmado, semântica/unidade pendente.
5. OBS-001/EXP-001: cobertura declarada e histórico não correspondem a evidência atual.

## Fatos e classificação
| Diferença | Classe |
|---|---|
| custom A IDs vs W3C/OTel B IDs | CORRELATION_TREATMENT |
| campos nativos/SDK e estrutura | REPRESENTATION_ONLY / STRUCTURE_TREATMENT |
| auto metric families B em RAW | audit-only se não model-visible |
| zero logs F4 no Dataset | DIAGNOSTIC_INFORMATION_DIFFERENCE / falta de observação contratual demonstrada |
| counter no cutoff diferente e convergência posterior | possível collection timing; precisa regra |
| commits/gerações de artifacts diferentes | provenance difference |

## Builder e complexidade
Metric mapping, janela, deltas, compactação e projeção são justificáveis. Classificadores textuais de logs são em parte obsoletos depois do gate shared. Marker pode ser adequado, mas precisa sobrevivência validada. Canonical outcome ainda protege formatos nativos; não deve esconder ausência/duplicação. Várias allowlists e policies criam drift. Builder não deve equiparar A/B removendo fatos exclusivos; deve falhar quando falta observação prevista.

## Validators mínimos
1. Deployment provenance.
2. Ground Truth/fault control plane.
3. Source observation parity.
4. Dataset/window/temporal contract.
5. Dataset→RCA projection + leakage.
Hash/package integrity faz parte do empacotamento. Um agregador único deve tornar gate ausente ou FAIL impeditivo.

## Confiança histórica
Resultados de fault/GT antigos são úteis para a versão executada, não paridade do HEAD. Pacote teste commit 6f1cfae é antigo e FAIL. F4 mais recente confirma RAW/GT/injeção mas invalida source-parity claim. Não houve bateria final confiável da geração atual.

## Topologia alvo
Manter dois adapters e backends separados; contract pequeno com request/downstream/datastore/error event; metric/traces partem de mesmas fronteiras sem IDs compartilhados; RAW completo; Builder faz janela/semântica/tempo/compactação/projeção; cinco gates mais package integrity; imagem com revision/digest verificados. Não implementar agora.

## Próxima execução física
Ainda não. Requisitos: revisão OCI por Pod, builds de todas linguagens, payload shared presente em backend A/B, source parity fail-closed, semantics request/attempt definida, testes e working tree/commit congelados. Ver detalhes em architecture-target.md e remediation-plan.md.

