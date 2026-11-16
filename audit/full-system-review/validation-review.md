# Revisão de validações e testes

| Validador | Propriedade | Limitação |
|---|---|---|
| shared_observation_coverage.py | busca estática de declarações | não prova compilação/runtime |
| observability_contract_audit.py | inventário RAW e conceitos | parte da conclusão deriva do próprio Builder |
| Ground Truth/fault | estado/control plane | necessário e independente de telemetria |
| Dataset contract | schema/janela/semântica | precisa falhar evento contratual ausente |
| canonical log/span parity | conceitos normalizados | classificador compartilhado pode mascarar bug |
| source observation parity | emissão na origem | essencial, mas Fresh F4 não bloqueou runner |
| provenance | Dataset→request | parcialmente independente, policy semelhante |
| leakage | campos proibidos | manter |
| package/hash | integridade de pacote | não prova validade científica |
| sampler/report | seleção/relatório | usa canonicalizadores de produção |

O runner Fresh F4 retornou PASS mesmo quando source audit falhou, marker era zero e Dataset tinha zero logs. Esse é o defeito de validação prioritário. Cobertura estática declara dez serviços completos enquanto fault coverage tem BLOCKED/PARTIAL. Há validation sprawl e status contraditórios.

## Testes desta auditoria

Python: 103 PASS usando runner unittest por imports. Node paymentservice/currencyservice: 10 PASS. Go e .NET: não executados, toolchains ausentes. Não declarar build .NET aprovado. A primeira tentativa de unittest discovery não correspondia aos imports da suíte e foi descartada; o runner correto passou. Testes unitários não cobrem fonte→serialização→backend→Builder nem provenance de imagem.

## Conjunto mínimo futuro

1. deployment provenance preflight;
2. Ground Truth/fault control-plane validation;
3. source observation parity em dados reais;
4. Dataset contract (janela, schema, temporal);
5. Dataset→request provenance e leakage, com estados independentes.

Package hash é publicação. Cada gate deve ter um agregador único e falhar se não executado.

