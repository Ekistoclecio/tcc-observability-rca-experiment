# Stage 13 — protocolo LLM/RCA e runner OpenAI

## Objetivo

Congelar a inferência antes de qualquer output e executar diagnósticos independentes, cegos e auditáveis sobre os RCA Inputs gerados por uma nova execução física.

## Protocolo normativo

O modelo principal é `gpt-6-luna`, OpenAI Responses API, reasoning `standard/medium`, service tier `default`, `store=false`, `stream=false`, verbosity `low`, `max_output_tokens=16384`, sem `temperature`, `top_p`, ferramentas ou estado conversacional. O request inclui instrução congelada, conteúdo JSON exato do RCA Input e Structured Output `json_schema` com `strict=true`.

`rca-prompt-v1.txt` e `rca-schema-v1.json` são as únicas fontes de verdade em `scripts/experiment/llm/resources/`. Seus hashes são verificados antes da construção de requests. A configuração versionada é `protocol-v1.json`; qualquer mudança posterior de prompt/schema/modelo/configuração exige uma nova versão de protocolo.

O prompt pede exatamente três hipóteses ordenadas, fault type de enum fechado e localização tipada (`service`, `service_edge`, `operation`, `datastore`). Cada hipótese inclui confidence, IDs de evidência e explicação curta, sem chain-of-thought. `response_validator.py` valida JSON Schema e semântica: ranks `[1,2,3]`, confidence não crescente, nullability por location kind, ausência de duplicatas location+fault e evidence IDs existentes no input. Respostas inválidas são preservadas como outcomes; nunca são reparadas.

## Blindness e geração de casos

O runner não abre Ground Truth. Depois de RCA Inputs novos serem gerados, `freeze_current_inputs()` cria um manifesto de hash da execução em results; o plano cego ordena inputs deterministicamente por hash e deriva IDs opacos. A mapping privado caminho/tratamento permanece em metadata local. Request model-visible não contém caminho, filename, ID de caso/fault, tratamento, Ground Truth, estado Chaos ou metadata experimental. Cada caso usa request independente, sem `previous_response_id` ou histórico compartilhado.

Credencial é carregada somente de `<repo>/.env` via `python-dotenv` com `override=False`; o arquivo precisa ser ignored e untracked. Secret não é impresso, serializado nem persistido. Testes usam segredo sintético e fake client.

## Runner, falhas e auditoria

`scripts/experiment/llm/runner.py` usa SDK OpenAI oficial com retries internos desabilitados. Até três tentativas com backoff 1/2 s aplicam-se apenas a falhas transitórias (conexão/timeout, 408, 409 transitório, 429 não-quota e 5xx). Erros de auth/permissão/request/quota encerram sem retry. Refusal, incomplete, output estruturado inválido e resposta semanticamente inválida são outcomes de uma chamada e nunca são repetidos. Esgotar transporte interrompe o piloto preservando progresso; resume não repete outcomes finalizados.

Por tentativa/caso são registrados tempos UTC e monotônicos, response ID/modelo/tier, status, tentativas, usage real (input/cache/output/reasoning/total), latência e custo estimado quando os detalhes permitem. Reasoning tokens não são somados novamente ao output. Preços vêm de snapshot versionado para o modelo/tier e custo fica nulo quando dados de cobrança são insuficientes.

`protocol.py`, `execution.py`, `accounting.py`, `credentials.py` e `response_validator.py` são módulos offline/testáveis; apenas comandos explícitos `control-smoke` e `run` fazem chamadas de rede. O evaluator não integra com OpenAI. As outputs de runs são geradas em diretórios da execução e não são mantidas neste repositório limpo.

## Problemas e decisões

- Dataset hash antigo não deve congelar novas execuções: hash manifest e execution plan são artifacts por execução, criados depois dos RCA Inputs atuais.
- O piloto de desenvolvimento foi usado para testar o fluxo e a política; seus inputs, predictions, usage e resultados foram removidos e não constituem resultados do TCC.
- Um teste temporário com outro modelo não muda o protocolo principal; a configuração versionada permanece `gpt-6-luna`, sem ramo Sol específico.

## Validação e estado

As suítes offline cobrem hashes, request leakage, carregamento de `.env`, ausência de rede, retries/resume, schema/semântica, accounting e mocked E2E. Nenhuma chamada real deve acontecer durante testes. Estado: runner implementado e pronto para uma nova execução; nenhum output anterior está armazenado.
