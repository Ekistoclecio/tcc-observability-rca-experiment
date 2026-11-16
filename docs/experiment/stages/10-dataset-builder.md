# Etapa 10 — Dataset Builder mínimo

## Resultado

**PASS para os RAWs canônicos F4 e F5 do commit `b790b6639532cedef93e357149ad96b204ab9ec0`.** O Builder transforma somente os bundles imutáveis apontados pela validação da Etapa 9. A transformação é determinística e cada fato aponta para arquivo, hash e JSON Pointer no RAW. Os fatos permanecem separados por cenário; contagens não foram equalizadas.

## Implementação

O comando é:

```bash
python3 scripts/experiment/build_dataset.py \
  --raw <bundle-raw> \
  --output-dir <diretório-vazio>
```

Implementação e testes: [`build_dataset.py`](../../../scripts/experiment/build_dataset.py) e [`test_build_dataset.py`](../../../scripts/experiment/test_build_dataset.py).

O pipeline é: verificar inventário/hash/permissões do bundle; ler métricas, logs e traces nativos; normalizar campos equivalentes; calcular deltas temporais de counters; compactar duration count/sum; produzir fatos e proveniência estáveis. Ground Truth não é lido pelo Builder.

Regras temporais:

- counter sem amostras suficientes ou sem baseline próximo ao início da janela fica `UNKNOWN`, nunca zero;
- reset de counter fica `UNKNOWN`;
- delta observado igual a zero é omitido;
- duração só é calculada quando count e sum estão observados;
- CPU é expressa em cores a partir do delta de CPU-seconds pela duração observada.

## Preservação e remoções justificadas

- A correlação `trace_id`/`span_id` de logs B é preservada; logs A não recebem esses campos.
- Atributos de aplicação estruturados presentes no RAW B, como `StatusCode` e `Detail`, são mantidos. Identidade de instância, escopo, SDK, severidade numérica e metadados do pipeline não viram fatos de aplicação.
- Mensagens de A e valores estruturados de B passam por remoção estrutural de linhas delimitadoras/frames de stack. O resumo do erro permanece. Caminhos, números de linha e stack do fault hook não entram no Dataset.
- Valores de campos de credencial/cartão reconhecidos por chave são redigidos; por exemplo, número, CVV e expiração do cartão sintético presentes no log de Charge. O fato de pagamento, moeda e valor permanecem.
- Famílias métricas não pertencentes ao contrato e spans de health/scrape/export permanecem no RAW, mas não são projetados como fatos diagnósticos do Dataset.

Essas regras não usam fault type, localização ou outro campo de Ground Truth. Não há transformação específica para F4/F5.

## Evidência canônica

Entradas RAW, intactas e verificadas:

| Fault | Bundle | Manifest SHA-256 | Hash-list SHA-256 |
|---|---|---|---|
| F4 | [`raw-b790b663/F4-bundle`](../09-raw-semantic-validation/raw-b790b663/F4-bundle/) | `3206fc74f26aab0b9c4d8bcbeb1f5f328bf4eaaa4ea960449363ea30ae822525` | `940de109df87c788628e1f7bda0424724cd89867a09c1e67c9866ac416fc0810` |
| F5 | [`raw-b790b663/F5-bundle`](../09-raw-semantic-validation/raw-b790b663/F5-bundle/) | `2baf85379adb0795101e183054538210869829ec13bc7fd90ada3d409ad665e2` | `a2672cf3720ad3f9533235390796fae8842d79627bc34b368c03f9f9bc3a2da3` |

Saídas auditadas: [`F4/`](10-dataset-builder/F4/) e [`F5/`](10-dataset-builder/F5/). O JSON [`10-dataset-builder-audit.json`](10-dataset-builder/10-dataset-builder-audit.json) registra resultados e hashes dos bundles.

| Fault | Fatos A | Fatos B | Observação |
|---|---:|---:|---|
| F4 | 304 | 282 | Diferença nativa preservada; um log `Payment charge failed` de severidade ERROR em cada cenário. O log A não tem IDs de trace/span; os IDs do log B apontam para o span real de erro Charge em B. |
| F5 | 170 | 165 | Diferença nativa preservada; cinco spans de erro `cartservice → redis-cart` em cada cenário. A operação/target e o erro estão representados sem copiar ou inferir um sinal a partir de outro. |

Em F5, o RAW representa detalhes de erro de maneiras diferentes: A inclui a mensagem renderizada; B usa `StatusCode`/`Detail` estruturados e o tipo de exceção no trace. O Builder conserva o resumo/atributos observados e remove stack/source location. Não iguala mensagens nem quantidades.

## Validação e gates

- **26/26** testes Python passaram, cobrindo transformação, determinismo de fixture, counter baseline/reset, normalização, remoção de stack, correlação B, redação e recusa de RAW alterado.
- F4 e F5 foram construídos duas vezes em diretórios independentes; os quatro arquivos de saída coincidiram byte a byte em cada execução.
- Auditoria independente resolveu todos os JSON Pointers e comparou cada SHA de fonte com `hashes.sha256`; todos os IDs de fatos têm entradas de proveniência e vice-versa.
- Scan das saídas: nenhum stack frame, caminho de origem/fault hook, Ground Truth, rótulo de cenário, identificador de execução ou PAN sintético exposto.
- F4 preserva fato de erro, severidade e relação log→span B; F5 preserva a relação de datastore e outcome ERROR nos dois lados.
- `git diff --check`: PASS.
- Nenhuma execução física nova foi necessária; foram usados somente os RAWs já validados da Etapa 9.

## Limitações conhecidas

- Dataset foi materializado para as janelas F4/F5 disponíveis; a janela normal da Etapa 8 continua sendo uma validação de instrumentação/RAW e não é um caso de fault para RCA.
- A lista de famílias métricas e os aliases semânticos correspondem às representações nativas atuais dos backends. Uma alteração de instrumentação exige nova RAW e revisão desses mapeamentos; não se deve ampliar o Builder para mascarar cobertura ausente.
- Counters sem baseline válido permanecem desconhecidos, o que reduz fatos disponíveis em vez de fabricar zero.

**Gate da Etapa 10: PASS.** Próxima etapa: contrato RCA, sem invocar modelo.
