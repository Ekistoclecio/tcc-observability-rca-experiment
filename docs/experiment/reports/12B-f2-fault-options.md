# F2 fault options — Etapa 12B

## Escopo e estado

- Branch: `experiment-v2`
- Commit testado: `7c1c1f8f1a076a9a10cef0afcfc83417a7c91fb5`
- Chaos Mesh: `v2.8.6`
- Execução física autorizada: somente `DIAGNOSTIC_F2_OPTION_A`; nenhum outro fault foi executado.
- Nenhum código, configuração permanente, fault definition, threshold, trace, metric, log, Builder ou validator foi alterado. Nenhum commit foi criado.
- A bateria oficial continua interrompida em F2 e inválida para composição. Uma bateria oficial futura deve recomeçar em F1.

## Topologia e causa atual

O checkout usa `paymentservice:50051`. Na execução inspecionada:

| Observação | Valor |
|---|---|
| `paymentservice` ClusterIP e porta | `10.96.106.113:50051` |
| Endpoint Pod IP | `10.244.0.37:50051` |
| checkoutservice Pod IP | `10.244.0.33` |
| peer do socket checkoutservice | `10.96.106.113:50051` |
| peer visto no socket servidor paymentservice | `10.244.0.33` |
| ipset do F2 atual | `10.244.0.37/32` |

O peer observado confirma que, no namespace do checkoutservice, o socket usa o ClusterIP. O diagnóstico 12A já provou que o matcher do F2 original continha somente o endpoint Pod IP: durante o checkout os pacotes passavam por `CHAOS-OUTPUT`, mas não casavam com o classificador e o qdisc netem permanecia sem tráfego. A causa atual continua `NETWORK_TARGET_MATCH_MISMATCH_PROVEN`.

## Chaos Mesh 2.8.6 e `externalTargets`

A inspeção do código da versão exata encontrou:

1. O campo `externalTargets` é descrito pela API como destino **fora do Kubernetes**. A documentação/código não declara o ClusterIP de um Service Kubernetes como caso de uso suportado.
2. Na implementação 2.8.6, `ApplyTc` resolve `externalTargets` e junta os alvos resolvidos aos Pods selecionados para construir os ipsets. `ResolveCidr` aceita `IP:porta`; `BuildIPSets` guarda entradas com porta no conjunto `hash:net,port`.
3. A validação dessa versão proíbe `externalTargets` para `direction: from` e `both` em ações netem, mas permite `direction: to`. O handler transforma `delay` em netem.
4. Se `target` e `externalTargets` forem usados juntos, os Pod IPs selecionados e os alvos externos são combinados no matcher. Para casar somente o VIP/porta do pagamento, o teste deixou `target` ausente e usou somente o `externalTargets` dinâmico.

Fontes do tag oficial `v2.8.6`: [NetworkChaos API](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.6/api/v1alpha1/networkchaos_types.go#L102-L120), [validação de direction](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.6/api/v1alpha1/networkchaos_webhook.go#L83-L108), [resolução e aplicação de TC](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.6/controllers/chaosimpl/networkchaos/trafficcontrol/impl.go#L265-L309), [parser IP/porta](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.6/controllers/podnetworkchaos/netutils/cidr.go#L51-L78) e [construção do ipset](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.6/controllers/podnetworkchaos/ipset/ipset.go#L35-L69).

**Conclusão de suporte:** a combinação ClusterIP/porta foi aceita e funcionou pelo caminho de implementação do Chaos Mesh 2.8.6, mas o uso de um ClusterIP em `externalTargets` é incidental em relação à descrição pública do campo; não foi possível classificá-lo como contrato oficialmente documentado para Services Kubernetes. O ambiente fixa Chaos Mesh 2.8.6, o que limita o risco de mudança não observada, mas não elimina essa ressalva.

## Option A — matcher do Service ClusterIP no checkoutservice

### Execução

Foi criado apenas durante a investigação um `NetworkChaos` temporário com:

```yaml
selector: app=checkoutservice
direction: to
externalTargets:
  - 10.96.106.113:50051
delay:
  latency: 100ms
  correlation: "100"
  jitter: 0ms
# target PodSelector omitido deliberadamente
```

A requisição de teste foi um fluxo normal do workload, com `POST /cart/checkout` HTTP 200. `Selected=True` e `AllInjected=True` foram observados. Antes da requisição, o matcher e netem tinham zero pacotes; depois:

| Contador no netns checkoutservice | Antes do checkout | Depois de um checkout |
|---|---:|---:|
| `CHAOS-OUTPUT` | 24 pacotes / 8.139 bytes | 225 pacotes / 37.126 bytes |
| regra `CLASSIFY` do alvo VIP/porta | 0 | **5 pacotes / 458 bytes** |
| qdisc netem configurado em 100 ms | 0 | **5 pacotes / 528 bytes** |

O ipset durante a injeção continha exatamente `10.96.106.113,tcp:50051` no conjunto `hash:net,port`; o conjunto Pod-IP estava vazio. Logo, pacotes reais do checkout casaram com VIP e porta, sem incluir o Pod IP como alvo alternativo.

### Latência observada na mesma execução física

Os traces A e B têm IDs independentes, como esperado, mas os spans de pagamento começaram com apenas 20 microssegundos de diferença e pertencem ao mesmo fluxo normal de checkout:

| Sinal | Baseline normal | DIAGNOSTIC_F2_OPTION_A | Aumento aproximado |
|---|---:|---:|---:|
| A `checkoutservice` CLIENT `PaymentService/Charge` | 3,716 ms | **103,346 ms** | +99,630 ms |
| B `checkoutservice` CLIENT `PaymentService/Charge` | 3,667 ms | **103,293 ms** | +99,626 ms |
| A CLIENT `ShippingService/GetQuote` | 0,930 ms | 2,312 ms | +1,382 ms |
| B CLIENT `ShippingService/GetQuote` | 0,843 ms | 2,255 ms | +1,412 ms |

Os spans do pagamento A/B são compatíveis com o atraso configurado de 100 ms. A dependência não alvo shipping continuou na faixa de poucos milissegundos e não recebeu atraso de 100 ms. O CLIENT span B estava presente no primeiro resultado consultado; não foi observada chegada tardia nesta execução. Isso não explica nem resolve a ausência histórica de spans B na bateria bloqueada: sua causa continua `B_TRACE_CAUSE_NOT_PROVEN`.

O fluxo externo completo retornou HTTP 200. O tempo de parede de `POST /cart/checkout` foi 299,031 ms no baseline salvo e 210,144 ms sob a opção A; esse agregado não acompanhou o aumento do CLIENT span por variar com o restante do caminho da aplicação. A evidência do fault é o contador de pacotes da aresta e o span CLIENT da chamada alvo, não o tempo agregado isolado da página.

### ClusterIP e recriação do cluster

O valor observado não deve ser fixado em configuração: ClusterIP pode ser estável durante a vida do Service, mas uma nova criação do cluster/Service pode atribuir outro IP. Uma implementação posterior teria de ler `spec.clusterIP` de `paymentservice` no namespace `online-boutique` imediatamente antes de criar a NetworkChaos e montar `IP:porta` dinamicamente. O fluxo dinâmico após recriação ainda precisa de validação; não foi testado nesta etapa.

### Recuperação

O recurso NetworkChaos foi removido, `tc` voltou a `qdisc noqueue`, não havia `NetworkChaos`/`StressChaos`, o SUT ficou 11/11 Ready e um `GET /` pós-recuperação respondeu HTTP 200. O controller deixou temporariamente um filtro e ipsets de diagnóstico em uma chain `TC-TABLES-0` sem referência. A chain inerte preexistia antes do teste com outro matcher; removi somente os ipsets próprios do diagnóstico e restaurei o matcher inerte preexistente. A evidência de pré/pós-restauração está em `option-a-cleanup-restoration.json`. Nenhum caminho ativo de packet processing permaneceu do teste.

**Assessment Option A:** `ACCEPTABLE`, com desempenho runtime e fidelidade do tratamento comprovados na versão fixada; ressalva de API não documentar ClusterIP e necessidade de resolução dinâmica/regressão após recriação.

## Option B — ponto pós-DNAT/lado paymentservice

O socket do paymentservice viu o peer do checkoutservice como Pod IP `10.244.0.33`, permitindo conceitualmente um `NetworkChaos` com pods selecionados por labels e matcher paymentservice→checkoutservice nesse ponto. A direção fisicamente alcançável por egress em paymentservice é a resposta `paymentservice → checkoutservice`; ela não atrasa os pacotes da chamada `checkoutservice → paymentservice` antes do pagamento.

Isso continua envolvendo o mesmo par de serviços e poderia aumentar a duração percebida pelo CLIENT span, mas muda a direção do tratamento e deixa o pedido gRPC sem atraso. Dado o Ground Truth dirigido `checkoutservice → paymentservice` e o requisito atual de `direction: to`, esta alternativa não é equivalente sem redefinir F2 como latência bidirecional/não dirigida. Não foi executada uma NetworkChaos reversa: a opção A alcançou diretamente os pacotes do pedido com fidelidade maior, e a alternativa B exigiria experimentar uma direção que não atende ao tratamento atual.

**Assessment Option B:** `POOR` para o desenho atual; tecnicamente plausível para a direção de resposta, mas insuficiente como implementação do F2 dirigido. Sem teste runtime, o match/efeito de B permanece `NOT_PROVEN`.

## Option C — TC/iptables direto no namespace do checkoutservice

Um controlador próprio poderia resolver o ClusterIP em cada execução e aplicar uma regra no egress do checkoutservice para destino ClusterIP + TCP porta 50051, classificando a classe `netem` de 100 ms. Isso observa o pacote antes do DNAT, não altera a aplicação/topologia e possui a mesma precisão pretendida por A. A infraestrutura atual já demonstra que o ponto dispõe de `iptables`/ipset/TC.

É um fallback tecnicamente viável, não testado nesta etapa. Ele exigiria código de plano de controle específico, interação direta com netns do runtime, idempotência, concorrência com os qdiscs do Chaos Mesh, recuperação após falha/interrupção e validação rigorosa de cleanup. A portabilidade fica ligada à implementação de rede do cluster. Não é a primeira escolha enquanto o caminho testado A permanecer disponível na versão fixada.

**Assessment Option C:** `ACCEPTABLE` como fallback se o uso de `externalTargets` para ClusterIP for rejeitado por não estar documentado; maior complexidade e risco de cleanup.

## Opções rejeitadas

- Atrasar todo outbound de checkoutservice: afeta outras dependências e serviços auxiliares, não apenas o edge alvo.
- `sleep`/delay dentro da aplicação: altera latência de aplicação, não latência de rede.
- Trocar `PAYMENT_SERVICE_ADDR` para Pod IP: modifica a topologia/comportamento do SUT para acomodar o fault.
- Criar Headless Service somente para F2: mudança artificial de topologia.
- Hardcode de ClusterIP/Pod IP: não sobrevive com segurança à recriação do cluster.

## Comparação

| Critério | Option A: VIP no checkout | Option B: egress payment→checkout | Option C: TC/iptables direto |
|---|---|---|---|
| Match real traffic | GOOD — 5 pacotes casados, runtime | NOT_PROVEN — peer Pod IP observado | GOOD em desenho, não runtime-testado |
| Edge specificity | GOOD — ClusterIP + porta | GOOD para a direção de resposta | GOOD — VIP + porta explícitos |
| Efeito de 100 ms | GOOD — spans +99,6 ms | NOT_PROVEN | NOT_PROVEN |
| Mesma execução física A/B | GOOD — spans da mesma request | POSSIBLE, não testado | POSSIBLE, não testado |
| Capacidade nativa Chaos Mesh | ACCEPTABLE — código funciona, uso VIP não documentado | GOOD — selectors de Pods/direção nativos | POOR — custom networking |
| Mudança de topologia | GOOD — nenhuma | GOOD — nenhuma | GOOD — nenhuma |
| Código de rede customizado | GOOD — nenhum | GOOD — nenhum | POOR — necessário |
| Confiabilidade de cleanup | ACCEPTABLE — qdisc recuperou; estado inerte exigiu restauração ao pretest | NOT_PROVEN | POOR/NOT_PROVEN — rollback próprio |
| Robustez a recriação | ACCEPTABLE se ClusterIP for consultado dinamicamente; não revalidado após recriação | GOOD em princípio via selectors; não testado | ACCEPTABLE se IP/Pods forem resolvidos a cada run |
| Fidelidade metodológica ao edge dirigido | GOOD — pedido checkout→payment foi atrasado | POOR — atraso só na resposta inversa | GOOD — pedido checkout→payment pode ser alvo |
| Complexidade | GOOD — menor | GOOD — simples, mas semântica divergente | POOR — maior |

## Recomendação

**Opção preferida: A, condicionada a manter Chaos Mesh fixado em 2.8.6 e resolver dinamicamente `paymentservice.spec.clusterIP` por execução.** É a menor opção testada que casa o pacote real no ponto source antes do DNAT, limita o match a `ClusterIP:50051`, produz os ~100 ms esperados no CLIENT span de A e B e deixa shipping sem atraso semelhante. Não exige alterar o desenho científico.

A ressalva é explícita: ClusterIP em `externalTargets` não consta como uso oficialmente documentado; sua seleção é suportada pelo caminho de implementação da versão fixada e foi comprovada em runtime neste ambiente, mas merece uma validação de regressão ao implementar e após recriar o cluster. Se a implementação precisar depender estritamente da semântica documentada da API, usar C como alternativa ou confirmar com os mantenedores antes da implementação definitiva.

Option B não é recomendada para o tratamento atual, pois atrasa a resposta no sentido oposto. A bateria anterior permanece inválida; após uma eventual correção e validação, a campanha oficial futura recomeça em F1.

## Resultado e cleanup

- Preflight do teste: cluster Kind experimental exclusivo em execução; 10/10 Pod image references/imageIDs iguais à evidência de provenance 10/10 do commit; 11/11 SUT Ready; backends A/B Ready; zero Chaos inicial; checkout normal passou.
- Option A: `Selected=True`, `AllInjected=True`, checkout HTTP 200, matcher 5 pacotes, netem 5 pacotes, spans A/B ~103,3 ms; dependência shipping sem efeito de 100 ms.
- B CLIENT span: presente no primeiro resultado consultado; a causa da ausência histórica permanece não provada.
- Cleanup: recurso temporário removido; qdisc `noqueue`; 11/11 Ready; zero CRs Chaos; GET `/` HTTP 200; filtro/ipsets do diagnóstico removidos e estado inerte preexistente restaurado.
- Não foram executados F1, F3, F4, F5, workload longo ou bateria oficial. Nenhum commit foi criado.
- Os passos iniciais de investigação que falharam ao localizar o nome do container do node não aplicaram fault nem modificaram o cluster; foram corrigidos antes da única execução Option A.

## Artefatos

`docs/experiment/reports/12B-f2-fault-options-artifacts/` preserva o manifest temporário, workload baseline e diagnóstico, respostas Jaeger A/B dos traces nativos, estados de ipset/iptables/TC antes/durante/depois e a restauração do estado inerte preexistente.
