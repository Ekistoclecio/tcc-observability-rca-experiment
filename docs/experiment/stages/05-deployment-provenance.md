# Stage 05 — proveniência do deploy

## Objetivo

Demonstrar a cadeia do source revision até a imagem efetivamente executada pelos Pods dos dez serviços de aplicação.

## Implementação

O build injeta revision Git em labels OCI; o verifier relaciona commit, metadados da imagem local, digest/config no containerd do node Kind, `imageID` do Pod e revision em execução. `redis-cart` é upstream fixado por digest e não pertence às dez imagens construídas do source; é parte da saúde do SUT, mas não do denominador de provenance de aplicação.

O verificador compara identificadores da mesma classe (config digest/repo digest) e trata formatos reportados pelo CRI, inclusive imagens `import-*`; não confunde Docker image ID, manifest digest e runtime imageID. Scripts e testes de provenance vivem em `scripts/experiment/` e `tests/experiment/`.

## Validação e invariantes

Provenance é gate antes das faults: os dez serviços devem apontar para o mesmo tested SHA. Mudança de código após freeze exige rebuild e novo SHA. Estado: cadeia Git→imagem→Pod automatizada, sem guardar manifests de deployments anteriores.
