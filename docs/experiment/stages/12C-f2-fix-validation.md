# Etapa 12C — correção e validação isolada do F2

## Estado

- Branch: `experiment-v2`.
- Commit da correção: `d6a43936b5b18ae7f6534f947c77bcba2a1f7bf3` (`fix(faults): target payment service ClusterIP for F2 latency`).
- SHA validado após build/deploy: `a51a09eae1c7b5cf8216ab96c4a6b15a1ce05dcd`.
- Chaos Mesh: `2.8.6`.
- Resultado isolado pós-deploy: **PASS**.

## Implementação

Antes de cada F2, o runner resolve `paymentservice.spec.clusterIP` no plano de controle e valida o Service, o endereço IPv4 e a porta TCP `50051`. O recurso NetworkChaos usa `checkoutservice` como origem e somente `externalTargets: <ClusterIP>:50051`; não seleciona o Pod IP como target. A direção continua `to`, com latência de `100ms`, jitter `0ms` e correlação `100%`.

O F2 só é confirmado depois de um probe de checkout controlado aumentar os contadores do matcher e do netem no namespace de rede do `checkoutservice`. Esse probe fica fora da janela confirmada de medição. A confirmação é do plano de controle/rede; spans não são usados como Ground Truth.

## Validação isolada pós-deploy

As dez imagens executadas passaram a verificação de provenance para o SHA `a51a09eae1c7b5cf8216ab96c4a6b15a1ce05dcd`. O ClusterIP foi resolvido dinamicamente como `10.96.106.113:50051`. Os contadores passaram de zero para quatro pacotes no matcher e no netem (460 bytes). O checkout de confirmação respondeu HTTP 200.

Na mesma execução física, os spans A/B de `checkoutservice` para `PaymentService/Charge` mediram `104.320ms` e `104.254ms`; os spans para `ShippingService/GetQuote` mediram `0.847ms` e `0.811ms`. O span CLIENT B estava presente. A coleta RAW da validação isolada passou sua verificação de integridade.

Após remover o fault, o NetworkChaos estava ausente, sem hook ativo, sem cadeia classifier referenciada e sem qdisc/netem ativo. O checkout normal pós-recovery passou. Foram observados ipsets de target órfãos e inertes; eles não tinham regra/classifier ativa associada e não foram tratados como fault residual.

O teste pré-commit também passou com o mesmo mecanismo: cinco pacotes incrementaram matcher/netem e spans A/B de aproximadamente `103.3ms`, sem aumento correspondente no caminho de shipping. Essa execução está identificada como validação pré-commit, não como bateria oficial.

## Bateria integrada posterior

A bateria oficial foi reiniciada em F1 com o commit `a51a09e...`; as validações individuais de F1–F5 e a validação semântica passaram. A bateria completa, entretanto, foi invalidada durante o empacotamento do RAW global porque `control/workload.json`, listado no hash list, não foi copiado ao staging. Esse blocker está documentado em [`BLOCKED-12-global-raw-packaging.md`](../reports/BLOCKED-12-global-raw-packaging.md). Os resultados dessa bateria não constituem resultados finais válidos.

## Limitação conhecida

Na versão fixada Chaos Mesh `2.8.6`, `externalTargets` com o Service ClusterIP/porta produziu o matcher necessário e foi confirmado por tráfego e contadores reais. A documentação da funcionalidade a descreve para alvos externos ao Kubernetes; o comportamento com ClusterIP é uma dependência da implementação validada nesta versão, não uma garantia universal nem presumida para versões futuras.

## Artefatos e gate

Evidências da validação pós-deploy estão em [`12C-f2-fix-validation-artifacts/post-deploy-f2/`](12C-f2-fix-validation-artifacts/post-deploy-f2/); a execução pré-commit está em `12C-f2-fix-validation-artifacts/precommit-f2/`.

Critérios da correção isolada: resolução dinâmica, matcher/netem incrementados por tráfego real, efeito de latência A/B na aresta pretendida, span B presente, caminho não relacionado sem aumento comparável, recovery e checkout normal pós-recovery. Todos passaram. Isso valida a correção F2 isolada; não remove o blocker de finalização da bateria integrada.
