# Etapa 5 — Proveniência do deploy

## Estado

- Branch: `experiment-v2`.
- Commit de implementação validado: `a1d9af68dce8428ef5a7d79c7499bfdb3dbb28b9`.
- Critério: demonstrar a cadeia `commit Git → imagem → Kind CRI → Pod em execução` para os dez serviços de aplicação.
- Registro da etapa incluído em commit documental posterior; a validação abaixo identifica explicitamente o commit que originou as imagens.

## Implementação

O build exige working tree limpa, usa o SHA completo de `HEAD` como tag imutável e grava esse valor no label OCI `org.opencontainers.image.revision`. As imagens são carregadas no Kind e verificadas no runtime antes de aplicar os manifests. Depois do apply, o script aguarda `kubectl rollout status` de cada serviço antes de executar a validação de Pods. Essa espera evita aceitar a condição `Available` herdada de um ReplicaSet antigo enquanto o rollout novo ainda está em andamento.

A validação compara separadamente as identidades de imagem. O digest da configuração extraído de `docker save` deve coincidir com o ID de configuração da imagem no CRI. O `imageID` do Pod deve coincidir com esse digest ou com o repo digest exato registrado pelo CRI para a mesma imagem. O label OCI é lido tanto da imagem local quanto da imagem no Kind.

## Serviços e evidência

Validação em `a1d9af68dce8428ef5a7d79c7499bfdb3dbb28b9`:

| Serviço | Revision OCI local/Kind | Pod e imageID correspondentes | Resultado |
|---|---|---|---|
| `frontend` | PASS | PASS | PASS |
| `checkoutservice` | PASS | PASS | PASS |
| `cartservice` | PASS | PASS | PASS |
| `productcatalogservice` | PASS | PASS | PASS |
| `currencyservice` | PASS | PASS | PASS |
| `paymentservice` | PASS | PASS | PASS |
| `shippingservice` | PASS | PASS | PASS |
| `emailservice` | PASS | PASS | PASS |
| `recommendationservice` | PASS | PASS | PASS |
| `adservice` | PASS | PASS | PASS |

O relatório estruturado em `05-deployment-provenance.json` contém, para cada serviço, o SHA esperado, referência de imagem, revisão OCI, digest de configuração local, ID CRI, Pod, `imageID` em execução e método de comparação. Resultado: `10/10 PASS`, sem serviço ausente ou divergência.

Após o rollout, `scripts/experiment/environment/verify.sh` confirmou `11/11` deployments `Ready` (os dez serviços e `redis-cart`) e `GET /` retornou HTTP 200.

## Validações executadas

- Oito testes unitários específicos de proveniência: PASS.
- `bash -n` nos scripts de deploy: PASS.
- `git diff --check`: PASS.
- Build das dez imagens com revision OCI completa: PASS.
- Carregamento e inspeção das dez imagens no Kind: PASS.
- Rollout completo dos dez serviços: PASS.
- Verificação independente local/CRI/Pod: PASS, 10/10.
- Checagem funcional normal do ambiente: PASS, 11/11 e HTTP 200.

## Limitações conhecidas

- `redis-cart` é uma imagem upstream fixada por digest e não faz parte das dez imagens de aplicação versionadas pelo commit. `loadgenerator` não é parte do SUT nem desta validação.
- `Docker image Id`, digest do manifest OCI e `imageID` do runtime podem representar objetos distintos. A validação usa o digest de configuração e a relação repoDigest verificada no CRI, sem comparar categorias diferentes diretamente.
- A imagem `adservice` pode ser reportada pelo kubelet como um repo digest `import-<data>`; esse valor foi validado contra o repoDigest e o ID de configuração da entrada CRI correspondente, além do label OCI de revision.

## Resultado

**PASS** — para o commit `a1d9af68dce8428ef5a7d79c7499bfdb3dbb28b9`, a revisão Git está presente nas imagens locais e no Kind, e os Pods em execução usam essas imagens. Nenhuma fault ou carga experimental foi executada nesta etapa.
