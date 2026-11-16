# Experimental Design and Invariants — TCC Observability RCA Experiment

## 1. Status e finalidade

Este documento define o desenho experimental, os invariantes metodológicos e os limites de implementação da versão `experiment-v2` do experimento utilizado no TCC:

> **Impacto da Qualidade da Telemetria no Diagnóstico de Falhas Assistido por IA em Arquiteturas de Microsserviços**

Este documento é a principal fonte de verdade para as decisões de implementação da nova linha experimental.

A implementação deve ser construída de forma incremental a partir destes princípios. Quando uma solução técnica conflitar com este documento, a implementação não deve ser adaptada silenciosamente para “fazer o teste passar”. A inconsistência deve ser identificada e discutida antes da alteração do desenho experimental.

A versão anterior do experimento permanece disponível apenas como referência histórica e técnica. A `experiment-v2` não deve preservar mecanismos da versão anterior apenas por compatibilidade.

Baseline da nova linha experimental:

```text
38e7348eb289eb5b87c0c6e8cb19ced0449dc389
```

---

# 2. Questão de pesquisa

A questão central do experimento é:

> **Como a qualidade e o nível de contextualização e correlação da telemetria influenciam a capacidade de sistemas baseados em IA realizarem análise de causa raiz em arquiteturas de microsserviços?**

---

# 3. Hipótese

A hipótese experimental é:

> **Telemetria mais estruturada, semanticamente contextualizada e explicitamente correlacionada pode melhorar o desempenho de sistemas baseados em IA na análise de causa raiz em comparação com telemetria multimodal produzida de forma mais independente e com correlação limitada ou implícita.**

A hipótese não assume que o cenário mais estruturado será necessariamente superior.

O experimento deve permitir que essa diferença seja medida.

---

# 4. Variável independente

O experimento **não compara Prometheus com OpenTelemetry**.

Também não compara:

```text
monitoramento vs. observabilidade
```

nem:

```text
metrics vs. traces
```

Os dois cenários possuem:

- métricas;
- logs;
- traces.

A variável independente é principalmente a forma como esses sinais são:

- instrumentados;
- propagados;
- estruturados;
- semanticamente padronizados;
- correlacionados entre si.

A comparação conceitual é:

```text
Scenario A
instrumentação convencional
sinais relativamente independentes
correlação explícita limitada

versus

Scenario B
instrumentação baseada em OpenTelemetry
contexto distribuído padronizado
maior estrutura semântica
correlação explícita entre sinais
```

---

# 5. Princípio fundamental do experimento

A comparação A/B deve ocorrer sobre a **mesma execução física da aplicação**.

Para uma operação real:

```text
real application execution
             |
       +-----+-----+
       |           |
       v           v
 Scenario A     Scenario B
```

Os dois cenários devem observar a mesma execução.

Não devem existir:

- uma execução para A;
- outra execução para B;
- faults independentes;
- workloads independentes.

Isso reduz diferenças provocadas por:

- timing;
- carga;
- comportamento da aplicação;
- retries;
- scheduling;
- infraestrutura.

---

# 6. Unidade experimental

A unidade experimental é uma execução controlada de um fault sobre o SUT submetido ao workload definido.

Cada execução deve possuir:

```text
workload
+
fault
+
Ground Truth
+
RAW telemetry A
+
RAW telemetry B
```

A aplicação e o fault são únicos.

A telemetria A e B é observada simultaneamente.

---

# 7. Sistema sob teste

O sistema base é o Google Online Boutique.

Serviços de aplicação considerados no experimento:

```text
frontend
checkoutservice
cartservice
productcatalogservice
currencyservice
paymentservice
shippingservice
emailservice
recommendationservice
adservice
```

Dependência de datastore pertencente ao SUT:

```text
redis-cart
```

O `loadgenerator` é componente de controle.

Ele:

- gera carga;
- não pertence ao espaço de diagnóstico;
- não pode ser considerado causa raiz;
- não deve fornecer evidência model-visible para RCA.

---

# 8. Scenario A — instrumentação convencional

O Scenario A representa uma arquitetura plausível de observabilidade construída com sinais relativamente independentes.

Características esperadas:

- métricas de aplicação instrumentadas diretamente;
- logs convencionais da aplicação;
- traces customizados ou convencionais;
- propagação de contexto própria quando necessária;
- ausência de correlação explícita geral entre logs, métricas e traces;
- ausência de dependência de semantic conventions do OpenTelemetry.

O cenário A pode possuir seus próprios IDs de trace/span.

Esses IDs pertencem ao seu sistema de tracing.

Eles não devem ser introduzidos artificialmente nos demais sinais apenas para aproximá-lo do Scenario B.

Em particular:

```text
A log <-> trace explicit correlation
```

não deve ser adicionada apenas para equalizar os cenários.

---

# 9. Scenario B — OpenTelemetry

O Scenario B utiliza OpenTelemetry como base de instrumentação e contexto distribuído.

Características esperadas:

- OpenTelemetry SDKs;
- propagação W3C;
- semantic conventions quando apropriadas;
- métricas OTel;
- traces OTel;
- logs contextualizados quando houver contexto ativo;
- `trace_id` e `span_id` nos logs quando naturalmente disponíveis;
- atributos de recurso como `service.name`.

O Scenario B pode possuir estrutura e contexto mais ricos que o Scenario A.

Essa diferença faz parte do tratamento experimental.

---

# 10. Independência das instrumentações

A instrumentação A e a instrumentação B devem observar as mesmas operações lógicas, mas permanecer implementações independentes.

É permitido utilizar o mesmo ponto natural do ciclo de vida de uma operação, por exemplo:

```text
request started
request completed
downstream call completed
datastore operation completed
```

para registrar os sinais dos dois cenários.

Entretanto, não deve existir uma abstração experimental intermediária que transforme previamente o fato em uma representação semântica comum e somente depois distribua essa estrutura para A e B.

Evitar arquitetura como:

```text
application
    |
SharedObservationEvent
    |
 +--+--+
 |     |
 A     B
```

quando esse objeto existir apenas para o experimento.

Preferir:

```text
application operation
        |
   +----+----+
   |         |
   A         B
instrumentation
```

O objetivo é manter uma instrumentação compatível com práticas comercialmente plausíveis.

---

# 11. Shared Observation Contract

Na `experiment-v2`, o conceito de **Shared Observation Contract** deve ser utilizado como contrato metodológico e de validação, não como uma camada obrigatória de runtime.

Ele define:

> Quais fatos diagnósticos ambos os cenários deveriam ser capazes de observar sobre uma determinada operação?

Ele não deve necessariamente criar esses fatos.

Exemplo:

```text
PaymentService.Charge failed
```

O contrato pode exigir que ambos os cenários permitam identificar:

```text
service = paymentservice
operation = Charge
outcome = ERROR
```

A representação pode ser diferente.

O Scenario B pode adicionalmente fornecer:

```text
trace_id
span_id
semantic attributes
```

O contrato é verificado posteriormente sobre a telemetria produzida.

---

# 12. Validade ecológica

A instrumentação deve permanecer plausível para uma aplicação comercial real.

Não adicionar sinais exclusivamente porque facilitam a validação do experimento.

Exemplos a evitar:

```text
shared_observation_test
experiment_event
scenario_a_event
scenario_b_event
fault_id
experiment_run_id
```

Não criar logs de sucesso apenas para produzir uma correspondência A/B se a aplicação normalmente não justificaria esse log.

É aceitável que operações normais sejam representadas principalmente por:

```text
metrics
+
traces
```

enquanto logs sejam utilizados principalmente para:

```text
errors
warnings
relevant application events
```

---

# 13. Paridade diagnóstica

O objetivo não é produzir telemetria byte a byte idêntica.

O objetivo é manter aproximadamente:

```text
diagnostic_information(A)
≈
diagnostic_information(B)
```

enquanto permitimos:

```text
structure(A) != structure(B)

correlation(A) != correlation(B)

context(A) != context(B)

semantic_standardization(A) != semantic_standardization(B)
```

A diferença entre os cenários deve resultar principalmente da qualidade de estruturação, contextualização e correlação.

---

# 14. Diferenças permitidas entre A e B

São diferenças esperadas do tratamento:

- formato de IDs;
- sistema de propagação;
- nomes nativos de atributos;
- semantic conventions;
- atributos de recurso;
- estrutura dos registros;
- correlação explícita de logs com traces no Scenario B;
- relações contextuais adicionais derivadas do contexto distribuído;
- representação nativa das métricas;
- representação nativa dos spans.

Essas diferenças não precisam ser removidas dos RAWs.

---

# 15. Diferenças não permitidas

Não devem existir diferenças sistemáticas como:

- A não observar uma operação diagnóstica importante observada por B;
- B observar uma causa real que A não instrumenta;
- apenas um cenário possuir o único log que revela diretamente a causa;
- apenas um cenário possuir métricas essenciais para um fault;
- apenas um cenário possuir a chamada downstream necessária para localizar F2/F3;
- apenas um cenário observar a operação de datastore necessária para F5.

Diferenças desse tipo são:

```text
DIAGNOSTIC_COVERAGE_DIFFERENCE
```

e devem ser tratadas na instrumentação, não escondidas posteriormente pelo Dataset Builder.

---

# 16. Métricas — contrato mínimo

Os conceitos mínimos de métricas do experimento são:

```text
request_count
request_duration
cpu_utilization
```

A implementação nativa pode variar entre A e B.

Quando aplicável, devem existir dimensões semanticamente equivalentes para:

```text
service
operation
outcome
target
```

Não é necessário que os nomes físicos das métricas ou labels sejam iguais.

Exemplo:

```text
A:
request_duration_seconds

B:
http.server.request.duration
```

pode ser válido se ambos representarem o mesmo conceito experimental.

---

# 17. Semântica temporal das métricas

Counters devem ser tratados temporalmente.

O experimento não deve usar um valor histórico acumulado como evidência direta de uma execução.

Para counters:

```text
delta = end_value - baseline_value
```

ou transformação semanticamente equivalente.

Regras obrigatórias:

```text
missing baseline != zero
```

Se não existir baseline válido:

```text
missing_baseline
```

deve permanecer desconhecido.

Nunca inferir zero.

Um delta válido igual a zero pode ser representado como zero ou omitido na visão compactada, desde que a regra seja:

- determinística;
- documentada;
- aplicada igualmente a A e B.

---

# 18. Export e scrape lag

Diferenças temporárias causadas por:

- export;
- buffering;
- scrape;
- backend ingestion;

não devem ser imediatamente classificadas como diferenças de instrumentação.

Entretanto:

```text
"é apenas lag"
```

não pode ser utilizado sem evidência.

A implementação deverá definir posteriormente uma regra explícita para:

- cutoff;
- drain;
- collection delay;
- fechamento da janela.

Essa regra deve ser comum aos dois cenários e independente do fault.

---

# 19. Logs — contrato

Logs model-visible devem representar fatos reais de aplicação.

São candidatos naturais:

- erros;
- warnings;
- eventos diagnósticos relevantes.

Logs de:

- runtime;
- SDK;
- framework;
- lifecycle;
- health checks;

não devem ganhar relevância diagnóstica apenas porque um dos cenários os produz em maior quantidade.

Não existe obrigação de gerar um log para toda operação bem-sucedida.

Uma operação pode ser perfeitamente representada por:

```text
metric + trace
```

sem log.

---

# 20. Logs e correlação

Quando um log de aplicação for produzido sob um contexto de trace:

Scenario A pode apresentar:

```text
timestamp
service
severity
message
operation
error
```

Scenario B pode apresentar os mesmos fatos e adicionalmente:

```text
trace_id
span_id
resource attributes
```

A correlação adicional em B é parte da variável experimental.

Não adicionar `trace_id/span_id` artificialmente ao Scenario A.

---

# 21. Traces — contrato

Os dois cenários devem observar as mesmas **boundaries lógicas relevantes**.

As principais categorias são:

```text
SERVER
CLIENT
DATASTORE
```

Quando aplicáveis.

Exemplos:

```text
incoming checkout request

checkoutservice -> paymentservice

cartservice -> redis-cart
```

A comparação deve ocorrer por significado lógico.

Não comparar:

```text
trace_id A == trace_id B
```

nem:

```text
span_id A == span_id B
```

Os IDs são independentes.

---

# 22. Auto-instrumentation

Auto-instrumentation no Scenario B deve ser utilizada com cautela.

Ela não pode criar uma vantagem diagnóstica não relacionada à variável estudada através de uma quantidade substancial de spans adicionais que revelam operações ausentes em A.

Spans automáticos podem permanecer nos RAWs.

Entretanto, antes de serem considerados model-visible, deve ser avaliado se representam:

```text
EXPECTED_TREATMENT_DIFFERENCE
```

ou:

```text
DIAGNOSTIC_COVERAGE_DIFFERENCE
```

---

# 23. Fault matrix

O experimento utiliza cinco classes de falhas.

| Fault | Tipo | Ground Truth location | Mecanismo |
|---|---|---|---|
| F1 | CPU saturation | `checkoutservice` | saturação controlada de CPU |
| F2 | Network latency | `checkoutservice -> paymentservice` | latência controlada na aresta |
| F3 | Service unavailable | `paymentservice` | indisponibilidade do serviço |
| F4 | Application exception | `paymentservice / Charge` | exceção controlada |
| F5 | Datastore unavailable | `redis-cart` | indisponibilidade do datastore |

F5 representa somente indisponibilidade do datastore.

---

# 24. Evidência mínima esperada por fault

## F1 — CPU saturation

Ambos os cenários devem permitir observar:

```text
high CPU at checkoutservice
+
request degradation associated with checkoutservice
```

---

## F2 — Network latency

Ambos devem permitir observar:

```text
increased duration
on checkoutservice -> paymentservice edge
```

A localização causal é a aresta.

---

## F3 — Service unavailable

Ambos devem permitir observar:

```text
failed call
checkoutservice -> paymentservice
```

e evidência compatível com indisponibilidade do `paymentservice`.

---

## F4 — Application exception

Ambos devem permitir observar:

```text
paymentservice
Charge
ERROR
```

A operação `Charge` faz parte do Ground Truth.

---

## F5 — Datastore unavailable

Ambos devem permitir observar:

```text
failed datastore interaction
cartservice -> redis-cart
```

A causa raiz é `redis-cart`.

---

# 25. Ground Truth

Ground Truth deve permanecer completamente independente da telemetria A/B.

Ele é produzido a partir do mecanismo de controle experimental.

O modelo de Ground Truth é:

```text
G_i = (L_i, F_i)
```

onde:

```text
L_i = root location
F_i = fault type
```

Ground Truth pode ser utilizado para:

- determinar elegibilidade da execução;
- definir timestamps do fault;
- definir janela experimental.

Não pode ser utilizado para:

- selecionar evidências;
- filtrar serviços causalmente relevantes;
- escolher métricas;
- escolher logs;
- escolher traces;
- ordenar hipóteses.

---

# 26. Fault window

A janela efetiva do fault é:

```text
confirmed_start
→
confirmed_end
```

Esses timestamps devem ser derivados do control plane.

A janela de coleta para RCA pode adicionar:

```text
pre_buffer
post_buffer
```

de maneira fixa e independente do fault.

---

# 27. Workload

O workload é componente de controle.

Ele deve ser:

- determinístico o suficiente para repetibilidade;
- externo ao SUT;
- independente dos cenários A/B;
- incapaz de revelar ao SUT qual fault está ativo.

Nenhum identificador como:

```text
run_id
fault_id
scenario
```

deve ser propagado para a aplicação com requests do workload.

---

# 28. RAW telemetry

RAW é a representação integral da telemetria coletada dos backends.

RAW deve ser:

- completo;
- preservado;
- auditável;
- separado por cenário;
- não normalizado;
- não equalizado.

É aceitável que:

```text
RAW size A != RAW size B
```

e:

```text
RAW signal count A != RAW signal count B
```

desde que a diferença não produza cobertura diagnóstica causal assimétrica no RCA Input.

---

# 29. RAW não deve ser equalizado

É proibido:

```text
remove N samples from B because A has fewer
```

ou:

```text
sample A/B until counts match
```

A quantidade bruta não é a variável controlada.

O controle experimental deve ocorrer no nível de:

```text
logical diagnostic facts
```

---

# 30. Dataset Builder

O Dataset Builder só deve ser implementado após a paridade semântica dos RAWs relevantes ter sido demonstrada.

O Builder deve ser uma transformação simples e auditável.

Responsabilidades legítimas:

```text
parse native telemetry
normalize equivalent field names
canonicalize equivalent outcomes
calculate temporal counter deltas
compact histograms/gauges deterministically
remove clearly non-diagnostic runtime/SDK metadata
produce stable evidence IDs
```

O Builder não deve corrigir instrumentação.

---

# 31. Proibições do Dataset Builder

O Builder não pode:

- adicionar fatos ausentes;
- reconstruir logs a partir de traces;
- reconstruir métricas a partir de traces;
- remover evidência porque o outro cenário não a possui;
- equalizar contagens;
- utilizar regras específicas por fault;
- utilizar Ground Truth para identificar a causa;
- selecionar apenas o serviço correto;
- ocultar diferenças reais de cobertura.

Se uma diferença exigir esse tipo de lógica:

```text
STOP
```

e revisar a instrumentação.

---

# 32. Normalização

Normalização deve resolver diferenças de representação.

Exemplo legítimo:

```text
A:
result = "error"

B:
otel.status_code = "ERROR"

Common:
outcome = ERROR
```

Outro exemplo:

```text
A:
operation = Charge

B:
rpc.method = Charge

Common:
operation = Charge
```

Normalização não pode transformar uma ausência em presença.

---

# 33. RCA Input

O modelo de IA deve receber somente o Dataset produzido a partir da telemetria.

Não deve receber:

- Ground Truth;
- nome do fault;
- run ID;
- scenario ID;
- mecanismo de injeção;
- Chaos resources;
- Kubernetes control-plane state;
- paths de fault hooks;
- dados internos do experimento.

O modelo não deve saber se está analisando:

```text
Scenario A
```

ou:

```text
Scenario B
```

---

# 34. Contrato futuro de saída RCA

Cada execução deverá produzir exatamente três hipóteses ordenadas.

Cada hipótese deverá conter, no mínimo:

```text
location
fault_type
confidence
evidence_ids
explanation
```

Taxonomia de fault:

```text
cpu_saturation
network_latency
service_unavailable
application_exception
datastore_unavailable
```

Tipos de localização:

```text
service
service_edge
operation
datastore
```

---

# 35. Critério de acerto

Uma hipótese somente é considerada correta quando satisfaz os componentes obrigatórios do Ground Truth.

Por exemplo:

F2 exige:

```text
checkoutservice -> paymentservice
+
network_latency
```

F4 exige:

```text
paymentservice / Charge
+
application_exception
```

Não considerar correta apenas por citar um serviço próximo da causa.

---

# 36. Métricas futuras de avaliação

As métricas previstas incluem:

```text
Accuracy@1
Accuracy@3
MRR
Location Accuracy
Fault Type Accuracy
Diagnosis Latency
Evidence Precision
Hallucination Rate
```

Custos e tokens podem ser métricas auxiliares.

---

# 37. Proveniência de deployment

Nenhuma execução experimental é válida se não for possível provar:

```text
Git commit
→
Docker image
→
running Pod
```

Cada imagem dos dez serviços deve carregar:

```text
org.opencontainers.image.revision
```

correspondente ao commit congelado.

A imagem efetivamente executada pelo Pod deve ser comprovadamente aquela imagem.

Deployment provenance é:

```text
HARD BLOCKER
```

Se falhar:

```text
DO NOT RUN FAULT
```

---

# 38. Working tree

Uma bateria oficial deve iniciar com:

```text
git status --porcelain
```

vazio.

Não executar uma bateria oficial a partir de working tree dirty.

---

# 39. Backend isolation

Os cenários devem manter backends separados.

Conceitualmente:

```text
Scenario A
Prometheus A
Loki A
Jaeger A

Scenario B
Prometheus B
Loki B
Jaeger B
```

Nenhum backend de um cenário deve receber intencionalmente sinais do outro.

---

# 40. Validação antes do Builder

Antes de construir qualquer Common RCA View, deve ser possível comparar RAW A/B e responder:

```text
same logical operations?
same important error facts?
same required metric concepts?
same required trace boundaries?
```

As diferenças encontradas devem ser classificadas como:

```text
EXPECTED_TREATMENT_DIFFERENCE

REPRESENTATION_DIFFERENCE

RAW_ONLY_DIFFERENCE

DIAGNOSTIC_COVERAGE_DIFFERENCE

BUG

NOT_YET_PROVEN
```

---

# 41. Validação de faults antes do Builder

F4 e F5 devem ser utilizados inicialmente como smokes de instrumentação.

Primeiro avaliar RAW diretamente.

F4 deve provar que ambos observam:

```text
paymentservice / Charge / ERROR
```

F5 deve provar que ambos observam:

```text
cartservice -> redis-cart failure
```

Somente após isso o Builder deve ser considerado confiável.

---

# 42. Validators

A nova implementação deve evitar proliferação de validators sobrepostos.

Preferir poucos gates independentes e claros.

Conceitualmente:

```text
1. Deployment provenance
2. Environment readiness
3. Fault / Ground Truth validity
4. RAW semantic coverage
5. Dataset provenance and contract
6. RCA leakage
7. Artifact integrity
```

Deve existir um resultado final coerente.

Se um gate obrigatório falhar:

```text
overall = FAIL
```

Nenhum relatório secundário pode declarar uma execução experimental válida quando um gate obrigatório falhou.

---

# 43. Não medir sucesso por quantidade de testes

Uma suíte com muitos testes não prova validade metodológica.

Os testes devem provar invariantes relevantes.

Sempre distinguir:

```text
implementation correctness
```

de:

```text
experimental validity
```

---

# 44. Root cause antes de patch

Ao encontrar diferença entre A e B, documentar:

```text
Observed
Expected
First divergence
Root cause
Evidence
Correct layer to change
```

antes de implementar a correção.

Classificar a camada:

```text
DESIGN
BUILD
DEPLOYMENT
OBSERVATION_POINT
ADAPTER_A
ADAPTER_B
PIPELINE
BACKEND
BUILDER
NORMALIZATION
VALIDATOR
GROUND_TRUTH
WORKLOAD
```

Não corrigir sintoma em outra camada.

---

# 45. Instrumentation change exige nova execução

Se for alterada:

- instrumentação;
- código da aplicação;
- adapter;
- pipeline de export;
- configuração que altera os sinais produzidos;

RAW antigo não pode comprovar a nova instrumentação.

É necessária nova:

```text
build
→
image
→
deploy
→
physical execution
```

RAW antigo pode continuar sendo utilizado para testar transformações puramente posteriores, como alterações no Builder, quando metodologicamente válido.

---

# 46. Ausência não pode virar zero

Regra geral do experimento:

```text
UNKNOWN != ZERO
```

Exemplos:

```text
missing metric baseline
missing event
missing span
```

não podem ser transformados silenciosamente em evidência negativa.

---

# 47. Não derivar sinais entre modalidades

É proibido preencher ausência de um sinal utilizando outro apenas para obter paridade.

Exemplos proibidos:

```text
missing metric
→ derive from traces

missing log
→ reconstruct from span

missing trace
→ infer from logs
```

As modalidades podem se complementar no diagnóstico, mas não devem ser utilizadas para fabricar equivalência de instrumentação.

---

# 48. Artificialidade experimental

Não adicionar ao código da aplicação:

```text
scenario=A
scenario=B
fault=F4
experiment=true
shared_event=true
```

como informação model-visible ou necessária ao funcionamento da observabilidade.

Mecanismos de controle experimental devem permanecer no plano de controle.

---

# 49. Compatibilidade histórica

A `experiment-v2` não possui obrigação de processar artifacts ou RAWs produzidos pela implementação anterior.

Não adicionar:

```text
legacy parser
legacy alias
compatibility fallback
```

apenas para manter compatibilidade com v1.

Compatibilidade somente deve existir quando possuir justificativa independente para o desenho atual.

---

# 50. Complexidade

Sempre preferir a menor implementação capaz de sustentar o invariante científico.

Antes de introduzir uma nova abstração, responder:

```text
What invariant does this abstraction protect?

Could the same invariant be enforced more simply?

Is this abstraction part of the experiment,
or only compensating for another design problem?
```

---

# 51. Política de implementação incremental

A nova implementação deve avançar em etapas pequenas.

Uma etapa deve:

1. possuir objetivo único;
2. possuir critério explícito de PASS;
3. ser validada antes da próxima;
4. evitar modificações em subsistemas não relacionados.

Não implementar várias fases experimentais em um único passo.

---

# 52. Sequência recomendada da v2

A sequência conceitual é:

```text
0. Experimental design and invariants

1. Reproducible environment

2. Deterministic workload

3. Fault injection + independent Ground Truth

4. Build/deployment provenance

5. Scenario A instrumentation

6. Scenario B instrumentation

7. Normal-operation RAW validation

8. F4/F5 RAW instrumentation smokes

9. RAW collection contract

10. Minimal Dataset Builder

11. Semantic validation

12. RCA Input contract

13. Integrated F4/F5 validation

14. Full F1-F5 battery

15. Freeze experimental pipeline

16. LLM/RCA evaluation
```

A ordem pode ser ajustada somente quando houver justificativa metodológica clara.

---

# 53. Critério para avançar de instrumentação

A implementação de A/B só deve avançar para o Builder quando houver evidência de que:

```text
same physical execution ........ PASS

required metrics A/B ........... PASS

required application error
facts A/B ...................... PASS

required logical trace
boundaries A/B ................. PASS

backend isolation .............. PASS

unexpected diagnostic coverage
difference ..................... NONE
```

---

# 54. Critério para executar bateria oficial

Antes da bateria F1–F5:

```text
working tree clean ............. PASS

deployment provenance .......... PASS

10 service images verified ..... PASS

backends healthy ............... PASS

fault cleanup .................. PASS

RAW collection validated ....... PASS

Builder frozen ................. PASS

validators frozen .............. PASS

RCA Input contract frozen ...... PASS
```

Durante uma bateria oficial não devem ocorrer:

- alterações de código;
- ajustes de configuração;
- retries seletivos;
- correções manuais;
- mudanças no Builder.

Se for necessária alteração:

```text
invalidate battery
→
fix
→
freeze new revision
→
start new battery
```

---

# 55. Artefatos

A estrutura final deve preservar claramente a separação entre RAW e dados utilizados pelo RCA.

Conceitualmente:

```text
audit/results/
├── README.md
├── datasetA/
│   ├── metrics.json
│   ├── logs.json
│   └── traces.json
├── datasetB/
│   ├── metrics.json
│   ├── logs.json
│   └── traces.json
├── 001_cpu_saturation_r01/
│   ├── datasetA.json
│   ├── datasetB.json
│   └── ground-truth.json
└── ...
```

Os diretórios globais de A/B representam RAW da bateria.

Os diretórios de fault representam os datasets da janela RCA.

---

# 56. Princípio científico central

A pergunta que deve orientar qualquer decisão de implementação é:

> **Qual é a menor implementação que permite afirmar, com evidência forte, que A e B observam os mesmos fatos diagnósticos relevantes, enquanto diferem principalmente na qualidade de estrutura, contextualização e correlação?**

Se uma implementação não ajuda a responder essa pergunta, sua necessidade deve ser questionada.

---

# 57. Definição de sucesso da implementação

O experimento não está correto apenas porque:

```text
all tests passed
```

ou porque:

```text
all validators returned PASS
```

A implementação é considerada metodologicamente adequada somente quando existe alinhamento entre:

```text
scientific design

implementation

code actually executed

telemetry actually produced

dataset transformation

RCA input

validation rules
```

O objetivo final não é produzir dois datasets artificialmente iguais.

O objetivo é construir dois cenários de observabilidade plausíveis, controlados e comparáveis, de modo que a diferença experimental relevante seja principalmente a qualidade de estruturação, contextualização e correlação da telemetria.