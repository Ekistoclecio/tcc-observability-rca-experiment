# Etapa 9 — validação semântica do RAW

## Resultado

**PASS.** Sobre os bundles RAW corrigidos e diretamente conferidos contra seus hashes, as 31 verificações semânticas passaram: métricas essenciais, cobertura dos limites de trace, fatos naturais de log, correlação B com spans reais, F4 e F5. Nenhuma evidência foi reconstruída de outro sinal ou selecionada pelo Ground Truth. Os bundles iniciais são mantidos como evidência histórica; seus traces A não foram usados porque um incidente de coleta foi provado e corrigido. Consulte [`INCIDENT-08-jaeger-port-forward-collision.md`](../reports/INCIDENT-08-jaeger-port-forward-collision.md).

## Código e proveniência

- Branch: `experiment-v2`.
- Base da execução das smokes: commit limpo `9b7d775bbc8b00857f7b04d3aebe63880d29fe41`.
- A validação de deployment das duas smokes registrou 10/10 imagens/Pods com revisão esperada `9b7d775...`; dados completos em `09-raw-semantic-validation/deployment-provenance.json`.
- O workload normal foi executado com as imagens cuja source fingerprint é `5504be74e58b032d72b32a9205b7199f75680bcecea99a87301ef67e04233a82`. A revisão não alterou os arquivos de aplicação/instrumentação em relação ao código de observabilidade já validado; foi uma janela normal da Etapa 8.
- O bug do coletor foi corrigido no commit `265aa87039964a412773a9046b7bba65f587c76f`. Os manifests corrigidos incluem commit do coletor, SHA-256 do script, fingerprint da aplicação, janela, portas/serviços confirmados e hashes RAW.
- Janelas: normal `18:10:56.314997Z–18:10:58.690015Z`; F4 `18:23:12.446519Z–18:23:22.120014Z`; F5 `18:24:56.041878Z–18:25:32.069947Z`, em `2026-11-16`.

## Fonte RAW e integridade

| Execução | Bundle utilizado | Séries A/B | Logs A/B | Arquivos RAW verificados |
|---|---|---:|---:|---:|
| Normal | [`08-raw-smoke/raw-corrected`](08-raw-smoke/raw-corrected/) | 217 / 1.375 | 152 / 136 | 44 respostas + manifesto e hashes |
| F4 | [`raw/corrected/F4`](09-raw-semantic-validation/raw/corrected/F4/) | 263 / 1.903 | 73 / 64 | 44 respostas + manifesto e hashes |
| F5 | [`raw/corrected/F5`](09-raw-semantic-validation/raw/corrected/F5/) | 298 / 2.063 | 89 / 48 | 44 respostas + manifesto e hashes |

Os diretórios contêm os corpos nativos de Prometheus, Loki e Jaeger. Não houve truncamento, reescrita dos corpos ou equalização de volumes. Cada bundle corrigido passou por `collect_raw.py --verify-existing` e pelo verificador de inventário, SHA-256 e permissões read-only da validação semântica. O JSON [`09-raw-semantic-validation.json`](09-raw-semantic-validation/09-raw-semantic-validation.json) contém as 31 verificações, hashes dos manifests, proveniência de cada bundle e os resultados detalhados. O RAW inicial permanece nos caminhos originais e não deve ser interpretado como trace A válido; não foi sobrescrito.

## Cobertura de operação normal

As métricas nativas A/B cobrem `request_count`, duração de requests e CPU nos dez serviços de aplicação; CPU também está presente para `redis-cart`. A cobertura de traces inclui operações SERVER nos 10/10 serviços em cada cenário e assinaturas semânticas iguais para 19 boundaries SERVER e 22 boundaries CLIENT. Inclui também os limites `checkoutservice → paymentservice` e `cartservice → redis-cart`.

As respostas RAW A/B têm 310/317 spans únicos e 12/15 trace IDs. B contém spans RAW adicionais de health check, scrape `/metrics` e exportação do próprio agente Java para o endpoint Zipkin de Jaeger A. Eles permanecem no RAW e foram classificados como `RAW_ONLY_DIFFERENCE`; não são operação causal exclusiva da aplicação. Depois dessa classificação estrutural, as assinaturas dos limites de aplicação requeridos coincidem. Não se exigiu igualdade de volumes brutos.

Logs naturais apareceram nos mesmos nove serviços A/B nesta pequena janela; `productcatalogservice` não emitiu logs naturais nela. Contagens: A=152, B=136. Os registros B correlacionados apontam para pares trace/span existentes em Jaeger B (136 verificados); não há IDs de trace/span em stdout/labels A e não foram encontrados registros B duplicados exatos. Não se adicionou um log de sucesso para preencher a ausência de logs de `productcatalogservice`.

## F4 — exceção de aplicação

Ground Truth veio do controle de fault, está elegível/recuperado e define `paymentservice/Charge`. O intervalo RAW coincide exatamente com `confirmed_start`/`confirmed_end`. Em cada cenário existe um registro natural `Payment charge failed`, de severidade semântica WARN, e um span SERVER de Charge com resultado de erro. O registro A não contém correlação explícita; o `trace_id`/`span_id` do registro B coincide com o span real de erro Charge em Jaeger B.

Os registros de exceção RAW incluem stack trace e o caminho `/usr/src/app/fault-hook.js` em A e B. Esse detalhe de implementação experimental não é evidência model-visible admissível. **Gate obrigatório da Etapa 10:** remover stack/source-location e referências ao fault hook do Dataset de ambos os cenários, preservando o fato real do erro, mensagem/tipo/outcome quando representáveis. O RAW permanece intacto. Isso é uma restrição para transformação, não uma falha de cobertura da Etapa 9.

## F5 — datastore indisponível

Ground Truth independente define `redis-cart`, status recuperado/elegível; a janela do bundle coincide com a janela confirmada. Cada cenário contém cinco spans CLIENT de erro em `cartservice` destinados a Redis. A representa destino/operações no nome (`redis-cart GET`, `redis-cart GET/SET`) e status por `result=ERROR`; B representa destino por `server.address=redis-cart`, `db.system=redis`, operações `GET`/`GET SET` e `RedisConnectionException`. A validação compara o mesmo limite e outcome sem inferir sinal de traces de outro cenário.

## Validação executada

- Coletor: 6 testes passaram; confirmam que um forward só é aceito após declarar exatamente a associação local/remota esperada.
- Validator semântico: 8 testes passaram, incluindo normalização das representações equivalentes de rota/frontend e Redis, classificação dos spans RAW-only, outcomes RPC e integridade do bundle.
- Validação integrada sobre os três bundles: **31/31 PASS**, `failed_checks=[]`.
- `py_compile` e `git diff --check`: PASS.
- Nenhum workload adicional foi emitido durante a reconsulta; as smokes físicas F4 e F5 executadas uma vez cada recuperaram o SUT e foram usadas apenas como validação RAW desta etapa.

## Critérios de gate

| Critério | Resultado |
|---|---|
| Métricas essenciais presentes em A e B | PASS |
| Cobertura SERVER 10/10 e limites CLIENT de aplicação sem assimetria crítica | PASS |
| A/B possuem mesmos fatos naturais de log observados; A sem correlação explícita | PASS |
| IDs B associados a spans reais e sem duplicação exata observada | PASS |
| F4: paymentservice/Charge erro em A/B; correlação B validada | PASS |
| F5: cartservice→redis-cart erro em A/B | PASS |
| Ground Truth e janela conferidos independentemente do RAW | PASS |
| Integridade dos bundles corrigidos | PASS |
| Diferenças críticas de cobertura sem explicação | NONE |

**Gate da Etapa 9: PASS.** A construção do Dataset começa na Etapa 10, usando apenas os bundles corrigidos indicados aqui. O stack do fault hook deve ser removido da projeção model-visible de forma simétrica; nenhum dado RAW pode ser editado. A política operacional continua exigindo somente um cluster Kind relevante ativo.
