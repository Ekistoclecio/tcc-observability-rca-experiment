# Linha do tempo técnica

HEAD analisado: d3670c83cf4f49764099e6f251bc238011595b62, branch main.

| Commit | Objetivo | Mudança | Estado atual |
|---|---|---|---|
| c6bad993 | base do pipeline | integração inicial | fundacional; só vale para sua geração |
| d6523258 | observabilidade dual | caminhos convencional e OTel | tratamento essencial, requer provar deploy |
| fb0e6c89 | captura/integridade | organização de coleta | mecanismo ainda relevante |
| 5058f2e3 | Dataset Builder | normalização por janela | necessário, mas acumulou responsabilidades |
| 56e248a1 | RCA Input | schema e request | contrato necessário |
| 2554e038 | temporalidade | delta de counters | necessário; passo de avaliação não é scrape |
| 0fcebc14 | observability contract | inventário e normalização | histórico, não fonte atual de paridade |
| 25741314 | span outcome | mapeamento de status | proteção útil, não deve ocultar origem divergente |
| 77a02d1a | logs/provenance | filtros e projeção | parte necessária, policy duplicada |
| 4248b042 | primeiros shared observations | callbacks em parte dos serviços | adoção incompleta |
| 4ee97c1a | shared gating | markers e gate de logs; alteração ampla | marker ausente na Fresh F4; imagem sem revisão comprovada |
| d3656c6d | métricas shared | amplia serviços | build/runtime não comprovados nesta auditoria |
| d3670c83 | coverage checker | busca estática por cobertura | lint preliminar, não runtime gate |

O padrão acumulado é: cada divergência originou normalizadores, filtros e validators sem uma prova forte de commit→imagem→Pod→backend. Não recomendo rollback amplo por volume. Primeiro deve-se estabelecer proveniência e então consolidar regras. 4ee97c1a não está validado como executado; isso não prova que o código esteja incorreto.

