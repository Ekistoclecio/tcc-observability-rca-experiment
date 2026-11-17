# Etapa 12P — validação temporal semântica

## Causa e correção

A bateria física executada com `1f0832a9864d0a0b5325221c7be738dec743fa37` permanece invalidada. Os gates F4/F5 falharam porque o validator comparava diretamente strings RFC3339. O Ground Truth registrava precisão de microssegundos (`...310126Z`) e o manifest representava o mesmo instante com nanossegundos (`...310126000Z`).

O validator agora interpreta os campos temporais conhecidos como instantes e os converte para nanossegundos inteiros. A comparação é exata, sem float, tolerância, truncamento ou reescrita dos artifacts. O diagnóstico inclui as strings originais, os instantes canônicos e o delta em nanossegundos. Timestamps inválidos ou com precisão inferior à resolução suportada falham explicitamente.

## Regressões

Os testes cobrem representações equivalentes entre microssegundos/nanossegundos, igualdade textual, diferenças reais de 1 ns e 1 µs, offsets de timezone equivalentes e divergentes, zeros fracionários finais, timestamps inválidos e os campos reais de janela do Ground Truth/manifest.

Os artifacts F4/F5 da bateria invalidada são usados somente como fixtures de regressão. Após a correção, a validação semântica neles passou (31 checks), sem alterar os arquivos preservados. A bateria antiga continua inválida; o novo commit requer nova bateria física desde F1.

Suíte local: 73 testes em `scripts/experiment` e 32 testes em `tests/experiment` passaram. `py_compile`, validação dos 760 JSONs preservados e `git diff --check` também passaram. Não houve mudança de manifests Kubernetes nesta etapa.

## Proveniência e limites

Esta alteração é restrita à comparação temporal do semantic validator e seus testes. Ground Truth, coleta RAW, manifests, telemetria, faults, Builder e RCA Input não são reescritos ou modificados por esta correção.
