# Etapa 6 — Scenario A convencional

## Estado e revisões

- Branch: `experiment-v2`.
- Base de aplicação construída e executada: `f59da69ca86421347d801fd9ce865d88a9349ff9` (`feat(observability): implement conventional scenario A`).
- Correções de coleta/infraestrutura A: `30383320db62ba2df440e7ad3114648fa4a9f79f` (`fix(observability): enable Promtail and cAdvisor collection`).
- As imagens dos dez serviços permanecem identificadas pela revisão completa `f59da69ca86421347d801fd9ce865d88a9349ff9`; o commit de correção altera apenas manifests de observabilidade, não código de aplicação.

## Implementação

Scenario A foi implementado sem OpenTelemetry. As aplicações usam instrumentação convencional própria para métricas de requisição, spans Zipkin e propagação customizada `x-tcc-trace-id` / `x-tcc-span-id`. Logs continuam sendo os registros reais de stdout das aplicações. Não há enriquecimento de logs A com IDs de trace/span.

O stack A está isolado no namespace `observability-a`:

- Prometheus `v3.5.0` coleta métricas diretas dos serviços e cAdvisor.
- Promtail/Loki `v3.5.5` coleta stdout CRI em Loki.
- Jaeger all-in-one `1.76.0` recebe spans pelo endpoint Zipkin.

Foi corrigido o DaemonSet do Promtail para derivar `HOSTNAME` de `spec.nodeName`. No Pod, o valor padrão era o nome do Pod (`promtail-a-…`), que não correspondia ao nó Kind; a descoberta `role: pod` permanecia em zero alvos. A configuração também passou a usar `/var/log/pods/*<pod_uid>/<container>/*.log`, padrão que corresponde aos diretórios reais do kubelet e evita a expansão ambígua de `$1_$2…`.

Foi concedido ao Prometheus somente `get` em `nodes/metrics`, necessário para o scrape direto de `https://<node>:10250/metrics/cadvisor`. A resposta anterior era `403` com autorização negada para `nodes` / subrecurso `metrics`; a chamada de leitura via API proxy retornava `200`, confirmando que o endpoint e os dados estavam disponíveis.

## Validação de runtime

Ambiente: Kind `v0.33.0`, node Kubernetes `v1.35.8`, Docker Engine `29.9.2`. O cluster experimental era o único cluster Kind ativo; o baseline permaneceu parado.

Após aplicar os manifests e executar `scripts/experiment/observability-a/verify.sh` com os binários fixados em `/tmp/tcc-experiment-v2-tools`:

- Jaeger, Loki, Prometheus: `1/1 Ready`.
- Promtail: `1/1 Ready`, sem argumento temporário de debug.
- Promtail descobriu 11 alvos (dez serviços e `redis-cart`) e manteve 11 arquivos ativos.
- A API do Loki retornou logs reais de `paymentservice`, incluindo cinco entradas `Transaction processed` na janela final observada.
- Prometheus apresentou `tcc_conventional_requests_total` e `tcc_conventional_request_duration_seconds_{sum,count}` para os dez serviços de aplicação.
- `sut-cadvisor` ficou `up`; a consulta de CPU retornou séries para os dez serviços e `redis-cart`.
- Jaeger listou operações para os dez serviços. Todos possuem boundaries SERVER; frontend, checkoutservice, cartservice e recommendationservice também expõem CLIENT. `cartservice` apresenta operações CLIENT `redis-cart GET`, `redis-cart SET` e `redis-cart GET/SET`.
- Logs A consultados não receberam campos `trace_id` ou `span_id` do Promtail; a correlação explícita log↔trace permaneceu ausente.

Foi executado workload benigno do host após o commit de configuração: uma rodada warmup e uma steady, cada uma com o fluxo completo `GET /`, produto, carrinho e checkout. Os dez requests retornaram HTTP `200`; não houve fault nem Chaos.

## Correções realizadas durante a etapa

1. Promtail sem alvos/arquivos: causa comprovada por comparação do `HOSTNAME` do processo com `spec.nodeName`, teste temporário de descoberta e observação da criação de 11 targets após definir o campo downward API. O caminho inicialmente gerado não correspondia ao layout real; a correção foi verificada por 11 tailers ativos e consulta direta do Loki.
2. CPU ausente: o scrape do kubelet retornava `403` porque o ServiceAccount tinha `nodes/proxy`, mas não `nodes/metrics`. A resposta de erro nomeava explicitamente o subrecurso ausente; após adicionar `get nodes/metrics`, o target ficou `up` e as séries apareceram.

Ambas as correções estão em `30383320db62ba2df440e7ad3114648fa4a9f79f`; não foi alterada instrumentação de aplicação, hipótese, fault ou regra de paridade.

## Testes e checks

- Build das imagens dos dez serviços e validação de provenance para a revisão de aplicação `f59da69ca86421347d801fd9ce865d88a9349ff9`: PASS, 10/10.
- Testes Go de frontend e productcatalogservice: PASS. Checkoutservice: `go test -vet=off ./...` PASS; o `go vet` padrão mantém aviso preexistente no `status.Errorf(codes.Internal, err.Error())`, presente antes das alterações desta etapa.
- Checagens de sintaxe Node/Python/Shell, build real dos serviços, `git diff --check` e render Kustomize do stack A: PASS.
- `scripts/experiment/observability-a/verify.sh` com Kind `v0.33.0` e kubectl `v1.35.8`: PASS.
- Workload benigno: PASS.
- Consultas diretas a Prometheus, Loki e Jaeger: PASS conforme evidências acima.

## Critérios da etapa

- [x] Métricas de requisição e duração disponíveis nos dez serviços.
- [x] Sinal CPU de cAdvisor disponível para o SUT.
- [x] Logs reais de aplicação chegam ao Loki A.
- [x] Spans SERVER nos dez serviços; CLIENT nas chamadas relevantes; DATASTORE no caminho cartservice → redis-cart.
- [x] Propagação customizada A mantida.
- [x] Sem correlação explícita geral entre logs e traces A.
- [x] Backends A saudáveis e isolados.
- [x] Operação normal conclui checkout sem fault.

## Limitações conhecidas para as próximas etapas

- cAdvisor expõe séries tanto no nível agregado do Pod quanto no container. RAW preserva ambas; o contrato semântico futuro deve selecionar uma única representação por Pod ou agregar sem dupla contagem. Não somar as duas famílias como se fossem medidas independentes.
- A exportação Zipkin usa resolução de microssegundos; Jaeger reportou alguns spans abaixo dessa resolução com duração zero e normalizou a visualização. Isso não impediu a observação das boundaries, mas duração de spans curtos deve ser examinada na validação semântica antes de usar duração como evidência.
- Logs de health/lifecycle e demais stdout permanecem no RAW por desenho. A coleta A não classifica nem filtra logs para produzir paridade.
- Scenario B, comparação A/B, Dataset Builder e RCA Input ainda não foram implementados nesta etapa.

## Resultado

**PASS — Scenario A está operacional em runtime sem fault.** A etapa fica registrada com a implementação `f59da69ca86421347d801fd9ce865d88a9349ff9` e a correção validada `30383320db62ba2df440e7ad3114648fa4a9f79f`.
