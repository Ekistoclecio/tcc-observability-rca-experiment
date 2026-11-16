# Revisão de proveniência de deploy

## Conclusão

O mismatch anterior 509360... local versus e28cbc... do Pod comparava digestes de tipos diferentes. O primeiro é o ID OCI/index local; o segundo é o digest de configuração da plataforma retornado pelo runtime. O Config da imagem local de paymentservice é e28cbc82..., igual ao imageID do Pod. A mesma coincidência config local ↔ Pod foi observada nos dez serviços.

Portanto não há evidência de conteúdo local/Pod diferente. Também não há prova do source commit embutido na imagem.

## Cadeia e lacunas

| Elo | Observação | Falta |
|---|---|---|
| source commit | runner grava Git SHA | SHA não está dentro da imagem |
| build | imagens locais e timestamps existem | atestado do contexto exato não existe |
| tag | tcc-phase2 | mutável |
| Kind load | build script constrói e carrega | pós-carga não compara revisão |
| manifest | aponta para tag | não fixa digest nem revisão |
| rollout | build script não força rollout geral | Pods antigos podem continuar |
| Pod | image e imageID disponíveis | imageID não prova commit |
| revisão OCI | não localizada/validada | vínculo commit→imagem ausente |

## Fresh F4 e marker

Fresh F4 tem cinco eventos payment_charge_failed por cenário nos RAWs, zero marker e zero logs no Dataset. Marker foi adicionado em 4ee97c1a depois dos timestamps observados de build/Pod de payment/cart. Promtail A só aplica estágio CRI; Collector B não mostra remoção do atributo. A hipótese mais provável, confiança alta, é imagem anterior ao marker. Root cause exata: ROOT_CAUSE_NOT_PROVEN, pois não há revision OCI nem manifesto imutável correlacionado ao Pod.

## Risco operacional

scripts/experiment/build-application-images.sh usa tags mutáveis, carrega imagens, mas não faz rollout geral. Manifests usam tcc-phase2 e IfNotPresent. setup-fault-injection.sh atualiza paymentservice de forma específica. integration_validation.py registra commit/readiness, mas não valida revisão embutida.

## Correção mínima futura

OCI label org.opencontainers.image.revision com SHA completo; deploy por digest/tag imutável; rollout explícito dos dez serviços; preflight que verifica revisão e digest do mesmo tipo em cada Pod; persistir manifesto e digests. Não comparar OCI index .Id com runtime config imageID.

