# Experiment V2

Este repositório contém a aplicação Online Boutique e a implementação reproduzível do experimento de observabilidade A/B e diagnóstico de causa raiz. O comando recomendado para uma execução normal é `./run-experiment.sh`; ele valida a configuração, prepara um runtime Kind exclusivo, executa a bateria configurada, publica uma run somente após os gates finais e remove o cluster experimental.

As execuções anteriores de validação de implementação não são o dataset definitivo. Uma run futura deve começar em ambiente limpo e será publicada em `docs/experiment/results/run-NNN/`.

## Arquitetura experimental

Cada instância física executa uma falha F1–F5 uma vez. O mesmo intervalo é coletado pelos tratamentos A e B. A coleta RAW nativa é validada semanticamente e alimenta o Dataset Builder e o RCA Input Builder. Se habilitados, os RCA Inputs são enviados independentemente ao modelo configurado e depois avaliados pelo evaluator determinístico usando o Ground Truth de controle, que nunca é enviado ao modelo.

O fluxo automatizado é:

```text
configuration and preflight
→ exclusive fresh Kind environment
→ deployment and provenance
→ configured physical fault instances
→ RAW A/B
→ semantic validation and builders
→ exact RCA requests
→ LLM responses, if enabled
→ deterministic evaluation, if enabled
→ integrity checks and atomic publication
→ runtime cleanup
```

## Sistema validado e requisitos

O ambiente operacional validado pelo projeto é **Windows com WSL2**, usando uma distribuição Linux e Docker Desktop com integração WSL habilitada. Execução nativa em Windows, macOS ou distribuições Linux arbitrárias não foi validada por este projeto.

Instale WSL2 em PowerShell como administrador e reinicie quando solicitado:

```powershell
wsl --install -d Ubuntu
```

O comando e os requisitos de Windows constam no [guia oficial de instalação do WSL](https://learn.microsoft.com/en-us/windows/wsl/install). Dentro da distribuição, atualize os pacotes e instale as ferramentas de sistema:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git python3 python3-venv python3-pip
```

Instale [Docker Desktop para Windows](https://docs.docker.com/desktop/setup/install/windows-install/), habilite o backend WSL2 e a integração com a distribuição utilizada. O projeto validou Docker Engine Server `29.9.2`; a instalação e configuração do backend WSL2 são descritas na [documentação oficial do Docker](https://docs.docker.com/desktop/features/wsl/). Confirme que o daemon está acessível **dentro do WSL**:

```bash
docker version
docker ps
```

O client e o server devem aparecer em `docker version`; `docker ps` deve terminar sem erro. O experimento requer Kind, kubectl, Docker, Python 3.10 ou mais recente, Bash e `curl`. Os testes de setup também usam `sha256sum`, `tar`, `grep`, `sed` e utilitários POSIX disponíveis no Ubuntu. O setup baixa Helm `3.17.3` se o binário pinado ainda não estiver disponível.

### Kind e kubectl fixados

O experimento exige Kind `v0.33.0` e kubectl `v1.35.8`. Instale os binários Linux x86-64 dentro do WSL:

```bash
sudo curl -Lo /usr/local/bin/kind https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64
sudo chmod +x /usr/local/bin/kind

sudo curl -Lo /usr/local/bin/kubectl https://dl.k8s.io/release/v1.35.8/bin/linux/amd64/kubectl
sudo chmod +x /usr/local/bin/kubectl
```

Os comandos seguem as instruções oficiais de [instalação do Kind](https://kind.sigs.k8s.io/docs/user/quick-start/) e [instalação do kubectl para Linux](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/), fixando as versões requeridas pelo repositório. Para ARM64, use os assets correspondentes da mesma versão; a execução completa nessa arquitetura não foi validada.

Verifique as versões e o runtime:

```bash
kind version
kubectl version --client
docker version
docker ps
```

As versões de Kind, Kubernetes node image, kubectl, Docker Engine, Helm e Chaos Mesh são versionadas em `config/experiment/environment/versions.env`. O runner verifica as versões exigidas e falha fechado em caso de divergência. O projeto não estabelece um mínimo universal de RAM, CPU ou disco; a máquina deve conseguir executar simultaneamente a aplicação, os backends A/B e Jaeger com limite de 512 MiB por instância.

## Clonar e preparar o repositório

No terminal WSL:

```bash
git clone https://github.com/Ekistoclecio/tcc-observability-rca-experiment.git
cd tcc-observability-rca-experiment

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r scripts/experiment/llm/requirements.txt

cp .env.example .env
```

Edite `.env` e forneça uma chave de API válida em `OPENAI_API_KEY` quando RCA estiver habilitado. `.env` é local, ignorado pelo Git e nunca deve ser commitado. Não inclua a credencial em comandos, código-fonte ou artifacts.

## Configurar a execução

`.env` contém somente seleção/configuração operacional. Intensidade e duração das falhas, workload, instrumentação, consultas, prompt, schema e regras de avaliação permanecem no código versionado.

### Repetições F1–F5

Cada variável indica o número de repetições físicas daquela falha:

```text
0 = desabilitada
1 = uma execução
N = N execuções, nomeadas R01, R02, ...
```

Exemplo pequeno, somente F1:

```dotenv
EXPERIMENT_F1_CPU_SATURATION=1
EXPERIMENT_F2_NETWORK_LATENCY=0
EXPERIMENT_F3_SERVICE_UNAVAILABLE=0
EXPERIMENT_F4_APPLICATION_EXCEPTION=0
EXPERIMENT_F5_DATASTORE_UNAVAILABLE=0
```

Desenho planejado completo, com cinco repetições por fault:

```dotenv
EXPERIMENT_F1_CPU_SATURATION=5
EXPERIMENT_F2_NETWORK_LATENCY=5
EXPERIMENT_F3_SERVICE_UNAVAILABLE=5
EXPERIMENT_F4_APPLICATION_EXCEPTION=5
EXPERIMENT_F5_DATASTORE_UNAVAILABLE=5
```

Contagens diferentes são permitidas. Por exemplo, `F1=3`, `F2=0`, `F3=2`, `F4=1`, `F5=4` produz 10 instâncias físicas e 20 casos A/B. Pelo menos uma fault precisa estar habilitada. Os valores aceitos são inteiros não negativos.

### RCA e avaliação

```dotenv
OPENAI_API_KEY=
RCA_ENABLED=true
RCA_MODEL=gpt-6-luna
RCA_REASONING_MODE=standard
RCA_REASONING_EFFORT=medium
RCA_MAX_OUTPUT_TOKENS=16384
RCA_SERVICE_TIER=default
EVALUATION_ENABLED=true
```

`RCA_ENABLED=false` encerra a execução científica depois de validar os RCA Inputs e não requer chave. `EVALUATION_ENABLED=true` requer `RCA_ENABLED=true`. Para uma execução RCA nova, `OPENAI_API_KEY` precisa estar disponível; chamadas ao modelo podem gerar custos de API. O runner também faz uma chamada sintética de compatibilidade (control smoke) antes das diagnoses; ela não é incluída nos resultados por case, mas pode gerar custo. Modelo, raciocínio, limite de saída e service tier são validados contra o registry versionado. Um modelo sem protocolo e pricing compatíveis falha antes da criação do ambiente; não há consulta dinâmica de preço.

O processo lê a `.env` da raiz com `override=False`: variáveis já definidas no ambiente do processo têm precedência. A mesma configuração resolvida é exibida antes de operações caras e registrada em `manifest.json`, sem incluir a chave nem qualquer derivação dela.

## Validar e executar

Para validar configuração e plano sem efeitos colaterais:

```bash
./run-experiment.sh --dry-run
```

O modo dry-run não consulta Kind, não cria cluster, não executa fault, não chama OpenAI e não cria uma run. Para executar a bateria configurada:

```bash
./run-experiment.sh
```

O runner exige runtime Kind exclusivo. Antes de iniciar, remova clusters Kind conflitantes; `tcc-baseline` é infraestrutura aposentada e não deve ser recriada. Nenhum cluster preexistente é encerrado automaticamente. O setup cria o cluster experimental limpo, instala Chaos Mesh na versão pinada, implanta a aplicação e observabilidade A/B e verifica provenance e saúde. Ao concluir ou falhar, o runner remove somente o cluster experimental identificado por esta configuração.

O pipeline falha fechado. Se um gate científico ou operacional obrigatório falhar, a execução para e não publica uma run como `SUCCESS`.

## Resultados

Cada execução bem-sucedida ocupa um ID livre (`run-001`, `run-002`, ...); uma run existente nunca é sobrescrita. O path é `docs/experiment/results/run-XXX/`:

```text
run-XXX/
├── manifest.json
├── ground-truth.json
├── raw/
│   ├── A/{metrics,logs,traces}/
│   └── B/{metrics,logs,traces}/
├── telemetry/
├── llm/
│   ├── inputs/
│   └── responses/
├── evaluation/results.json
└── hashes.sha256
```

- `manifest.json`: identidade da run, configuração resolvida, commit, protocolo e proveniência.
- `ground-truth.json`: Ground Truth derivado independentemente de evidência de execução/control plane.
- `raw/`: respostas completas e nativas de métricas, logs e traces por tratamento.
- `telemetry/`: saídas fault-specific do Dataset Builder com proveniência da RAW.
- `llm/inputs/`: requests científicos exatos model-visible, sem Ground Truth e sem wrapper de tratamento.
- `llm/responses/`: outcomes originais, prediction quando disponível, usage, custo, latência e validação.
- `evaluation/results.json`: métricas determinísticas por case, fault e overall.
- `hashes.sha256`: inventário de integridade de todos os arquivos da run, exceto ele próprio.

## Repetir uma execução

Revise a configuração em `.env` e invoque `./run-experiment.sh` novamente. O pipeline aloca o próximo `run-XXX` disponível e não apaga resultados anteriores. Preserve o commit e a configuração de cada run para comparabilidade; faults, workload, telemetria, builders, prompt/schema e semântica das métricas não são parâmetros do `.env`.

## Solução de problemas

- **Kind não encontrado/versão incorreta:** instale `v0.33.0`, confira `PATH` e execute `kind version`.
- **kubectl incompatível:** use `v1.35.8` e confira `kubectl version --client`.
- **Docker indisponível:** confirme Docker Desktop iniciado, integração WSL habilitada e sucesso de `docker version`/`docker ps` dentro da distribuição.
- **Cluster Kind preexistente:** remova manualmente clusters conflitantes antes de executar; o experimento exige runtime exclusivo.
- **Chaos Mesh indisponível:** verifique acesso ao Docker daemon/rede; o setup instala Chaos Mesh `2.8.6` e valida os controllers.
- **Jaeger reiniciando/OOM:** a execução para; investigue runtime/storage limpos. Não aumente limites nem altere parâmetros científicos como atalho.
- **`.env` ou chave ausente:** copie `.env.example`, defina `OPENAI_API_KEY` localmente e confirme que `.env` continua ignorado e untracked.
- **Modelo sem suporte:** selecione um modelo presente no registry de protocolo e pricing versionado; não reutilize preços de outro modelo.
