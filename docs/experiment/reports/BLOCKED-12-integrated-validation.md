# BLOCKED — Etapa 12 integrated validation (tentativa invalidada)

## Identificação

- Commit executado: `e7305f6ad0d22433311b162fd37fb0cdda1cd738` (`experiment-v2`).
- Escopo físico alcançado: workload normal e coleta RAW normal passaram; o runner parou na invocação de F1, antes de F2–F5.
- Esta tentativa é inválida como bateria integrada e não deve ser tratada como resultado experimental.

## Observado

O runner executou `fault_smoke.py` por caminho de arquivo. O subprocesso não produziu JSON; a primeira versão do runner descartou `returncode` e `stderr` ao reportar essa falha.

Uma invocação read-only de ajuda confirmou a causa: `python3 scripts/experiment/fault_smoke.py --help` termina na importação da linha 17 com:

```text
ModuleNotFoundError: No module named 'scripts'
```

O script importa `scripts.experiment.ground_truth`. Executado como arquivo, o diretório `scripts/experiment` ocupa a entrada inicial de `sys.path`, sem garantir o pacote `scripts` no caminho. O subprocesso falhou antes de entrar em `main`; o diretório preservado `F1/` não contém Ground Truth nem relatório de fault. A verificação read-only posterior encontrou zero recursos Chaos e 11/11 componentes SUT Ready.

## Esperado e primeira divergência

Esperado: iniciar o runner de fault, registrar a tentativa, injetar/confirmar F1, recuperar e emitir o resultado JSON.

Primeira divergência: **entrypoint Python do subprocesso de F1**, antes de qualquer evidência de injeção. Não houve avanço para F2–F5.

## Causa e impacto

- Root cause: **PROVEN** — invocação como arquivo incompatível com o import absoluto do pacote.
- Invariante afetado: a bateria integrada só pode prosseguir quando cada fault gera evidência completa e reportável.
- Impacto científico: nenhuma conclusão F1–F5 pode ser extraída desta tentativa. O workload e RAW normal ficam preservados apenas como evidência da tentativa inválida.
- Cleanup: zero recursos Chaos; SUT 11/11 Ready; nenhum F4 flag presente na última checagem de preflight. Cluster baseline permaneceu parado e intocado.

## Correção segura e repetição

A correção mínima é iniciar o subprocesso como módulo:

```text
python -m scripts.experiment.fault_smoke
```

O runner também deve preservar `returncode`, stdout e stderr quando o subprocesso não retornar JSON. Isso permanece dentro da etapa de orquestração e não altera fault, workload, instrumentação ou desenho científico.

Uma bateria nova completa exige novo commit, rebuild/deploy com proveniência desse commit e reexecução desde F1 até F5. Não se deve repetir apenas F1 nem combinar esta tentativa com outra.

## Evidências preservadas

- Artefatos pré-falha: [`BLOCKED-12-artifacts/`](BLOCKED-12-artifacts/)
- Preflight e proveniência 10/10: `BLOCKED-12-artifacts/results/preflight.json`
- Workload normal e RAW: `BLOCKED-12-artifacts/normal-workload.json`, `normal-raw/` e `normal-dataset/`
- Verificação posterior read-only: nenhum Chaos ativo; SUT 11/11 Ready.

Nenhuma fault além da tentativa de iniciar F1 foi executada; F2–F5 não começaram. Nenhum código foi alterado durante a tentativa física. Nenhum LLM foi executado.
