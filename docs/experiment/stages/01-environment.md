# Etapa 1 — Ambiente reproduzível

## Estado e commit

- Branch: `experiment-v2`.
- Revisão-base da etapa: `6427a0611ad005f19586edcd7d04a13f4e34f823`.
- Commit correspondente: `chore(environment): add reproducible Kind environment` (este registro é incluído nesse commit).
- Baseline metodológico: `38e7348eb289eb5b87c0c6e8cb19ced0449dc389`.

## Ambiente fixado

| Componente | Versão/configuração |
|---|---|
| Kind | `v0.33.0` |
| Kubernetes / node | `v1.35.8` |
| Imagem do node | `kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0` |
| kubectl | `v1.35.8` |
| Docker Engine | `29.9.2` |
| Google Online Boutique | `v0.10.7` |
| Redis | `docker.io/library/redis@sha256:47b08295737773de3b711fb86f13ae802f5074e96a9a2d29f5f80164ac08c046` |
| Cluster experimental | `tcc-experiment-v2-stage1` |
| Namespace da aplicação | `online-boutique` |

## Comandos

As ferramentas executáveis usadas nesta validação estavam em `/tmp/tcc-experiment-v2-tools`; no ambiente normal, os scripts usam `kind` e `kubectl` do `PATH`.

```bash
scripts/experiment/environment/create.sh
scripts/experiment/environment/deploy.sh
scripts/experiment/environment/verify.sh
scripts/experiment/environment/destroy.sh
```

`create.sh` cria o cluster somente se o nome ainda não existir. `deploy.sh` aplica a composição Kustomize baseada no Online Boutique v0.10.7, sem o loadgenerator. `verify.sh` espera os 11 deployments e faz um único GET funcional a `/` por port-forward. `destroy.sh` remove somente o cluster experimental.

## Exclusividade do runtime Kind

O setup consulta os containers ativos com o label `io.x-k8s.kind.cluster`. Se outro cluster Kind estiver ativo, falha antes de criar o cluster experimental e informa que a execução requer exclusividade por causa do limite compartilhado de instâncias inotify observado neste WSL. O script não para outros clusters.

O gate foi exercitado com `tcc-baseline` ativo: `create.sh` terminou sem criar cluster e reportou `Outro cluster Kind está ativo: tcc-baseline`. A tentativa foi repetida após a restauração do baseline e teve o mesmo resultado. Não foi necessário reproduzir o esgotamento de inotify. A evidência diagnóstica anterior localizou `inotify_init1 → EMFILE` no watcher fsnotify do kube-proxy; o limite compartilhado observado foi 128 instâncias. A regra operacional é manter apenas um cluster Kind ativo durante o runtime da experiment-v2. O ambiente não altera sysctls nem configurações do cluster de referência.

## Primeira criação limpa

Execução em 16/11/2026, com `tcc-baseline` parado. O baseline não foi removido, recriado nem modificado.

1. Criação do cluster: 08:25:24–08:26:03 (-03:00); node `Ready`, Kubernetes `v1.35.8`.
2. Antes do SUT, kube-proxy `1/1 Ready`, zero reinícios; CoreDNS `2/2 Ready`, zero reinícios.
3. Um Pod diagnóstico temporário resolveu `kubernetes.default.svc.cluster.local` para `10.96.0.1` usando DNS `10.96.0.10` e abriu conexão TCP ao Service `kubernetes:443`. O Pod foi removido.
4. Deploy: 08:26:35–08:28:33 (-03:00); os dez serviços de aplicação e `redis-cart` ficaram `1/1 Ready`.
5. `verify.sh`: HTTP `200` em `GET /`.
6. Um Pod diagnóstico temporário resolveu `currencyservice.online-boutique.svc.cluster.local` e abriu conexão TCP para `currencyservice:7000`. O Pod foi removido.
7. Teardown: 08:29:15–08:29:23 (-03:00); o cluster experimental ficou ausente. `tcc-baseline` permaneceu parado.

## Segunda criação limpa

Sem iniciar o baseline entre os ciclos, a mesma configuração e os mesmos scripts foram usados novamente.

1. Criação: 08:29:31–08:30:07 (-03:00); node `Ready`.
2. Antes do SUT, kube-proxy `1/1 Ready`, zero reinícios; CoreDNS `2/2 Ready`, zero reinícios. O Pod diagnóstico resolveu `kubernetes.default.svc.cluster.local` para `10.96.0.1` e conectou ao Service `kubernetes:443`; foi removido.
3. Deploy: 08:30:20–08:32:10 (-03:00); os dez serviços de aplicação e `redis-cart` ficaram `1/1 Ready`.
4. `verify.sh`: HTTP `200` em `GET /`.
5. Um Pod diagnóstico temporário resolveu `currencyservice.online-boutique.svc.cluster.local` e abriu conexão TCP para `currencyservice:7000`; foi removido.
6. Teardown: 08:32:39–08:32:44 (-03:00); o cluster experimental ficou ausente.

## Restauração do cluster de referência

Foi reiniciado o mesmo container de controle-plane `tcc-baseline-control-plane`, sem recriar o cluster. Depois da inicialização, o node ficou `Ready`, kube-proxy `1/1 Ready` e CoreDNS `2/2 Ready`. Os componentes tiveram um reinício adicional associado à parada forçada observada anteriormente; os logs confirmaram sincronização dos caches do kube-proxy e prontidão posterior do CoreDNS. No estado final, `kind get clusters` listou somente `tcc-baseline`.

## Validações e critérios

- [x] Gate de coexistência bloqueia antes de criar/alterar recursos.
- [x] Duas criações limpas com a mesma configuração.
- [x] Kube-proxy estável e CoreDNS `2/2 Ready` antes do SUT em ambos os ciclos.
- [x] DNS e conectividade ClusterIP do Service `kubernetes` antes do SUT.
- [x] 11/11 deployments Ready em ambos os ciclos.
- [x] `GET /` retornou HTTP 200 nos dois ciclos.
- [x] DNS e conexão ao `currencyservice` passaram nos dois ciclos.
- [x] Somente o cluster experimental foi removido; o mesmo baseline foi restaurado e verificado saudável.
- [x] `bash -n scripts/experiment/environment/*.sh` passou.
- [x] `kubectl kustomize --load-restrictor=LoadRestrictionsNone kustomize/experiment-v2` renderizou 11 Deployments e 12 Services.
- [x] `git diff --check` passou.

## Limitações conhecidas

- A parada do container baseline excedeu o timeout gracioso de 30 segundos e terminou com código 137; o container não foi removido. A restauração foi feita com `docker start` no mesmo container e a saúde foi confirmada antes de encerrar a etapa.
- O gate é específico para containers Kind ativos visíveis ao Docker daemon atual. Ele não tenta administrar clusters externos a esse daemon.
- Os Pods usados para sondagem DNS/rede são temporários e não pertencem ao SUT nem geram evidência experimental.

## Resultado

**PASS** — o ambiente foi criado duas vezes a partir do mesmo processo, serviu a aplicação, passou os checks de rede e foi removido sem alterar o cluster de referência.
