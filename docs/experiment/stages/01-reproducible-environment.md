# Stage 01 — ambiente reproduzível

## Objetivo e implementação

Provisionar isoladamente o Online Boutique em Kind com versões e imagens fixadas. Scripts: `scripts/experiment/environment/{create,deploy,verify,destroy}.sh`; composição em `kustomize/experiment-v2/`. A aplicação inclui dez serviços e `redis-cart`; `loadgenerator` não é parte do SUT nem fonte diagnóstica.

As versões-base registradas incluem Kind v0.33.0, Kubernetes/node v1.35.8, Online Boutique v0.10.7 e Redis fixado por digest. Imagens de aplicação são construídas localmente e rastreadas por provenance separada (Stage 05). `verify.sh` confirma os Deployments e uma requisição funcional sem executar fault.

## Coexistência WSL/Kind

O ambiente verifica containers Kind ativos antes de criar/remover seu cluster. Por limite compartilhado de instâncias inotify observado no WSL (falha `inotify_init1/EMFILE` no watcher fsnotify de kube-proxy), exige um único cluster Kind relevante ativo no runtime. O gate bloqueia sem parar clusters alheios, alterar sysctl ou ajustar ulimit. O teardown remove somente o cluster experimental identificado.

## Validação e invariantes

Validar shell syntax, render Kustomize, criação/deploy/health/HTTP e teardown. Pods de sondagem DNS/rede, se usados no diagnóstico, são temporários e não fazem parte do SUT. Estado: setup idempotente e pronto para criação limpa; estabilidade de telemetria é gate integrado no Stage 12.
