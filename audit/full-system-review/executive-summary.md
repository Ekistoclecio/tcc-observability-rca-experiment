# Resumo executivo

## Conclusão

O desenho científico pretendido continua defensável: uma única execução física é observada por duas rotas de telemetria independentes, A convencional e B OpenTelemetry, com propagação/correlação diferentes. Isso permite comparar estrutura e correlação sem comparar duas execuções de carga distintas.

A implementação, porém, **não está pronta para uma validação integrada confiável**. O problema prioritário é deployment provenance: a run registra o commit do repositório, mas não prova que os containers executaram esse commit. Na Fresh F4, os imageIDs dos pods correspondem ao config digest das imagens locais, mas as imagens não têm revisão Git. Os pods e imagens foram criados antes das alterações relevantes mais recentes. O marcador compartilhado ausente em ambos RAWs e os cinco eventos sem marcador são consistentes com imagem anterior ao gate do Shared Observation Contract.

A alegação anterior de que sha256:e28cbc... era diferente da imagem local sha256:509360... estava errada: eram tipos de digest diferentes (config da plataforma versus índice OCI). A igualdade de config digest foi verificada nos 10 serviços. Isso corrige a alegação do digest, mas **não prova qual commit foi compilado**.

## Avaliação direta

- **Pipeline conceitualmente correto?** Parcialmente. A/B são isolados por backend e alimentados pela mesma execução; a fonte exata executada não está atestada.
- **Excessivamente complexo?** Sim. O processo acumulou contratos e validadores que se sobrepõem; o maior custo agora é provar a cadeia de execução e não a quantidade de regras do Builder.
- **Maior bloqueador?** Imagem executada sem identidade imutável da revisão, junto com integração que declarou PASS sem exigir paridade de origem.
- **Ajustes ou refatoração parcial?** Ajuste operacional de provenance primeiro; depois consolidar a validação e remover heurísticas/compatibilidade redundantes.
- **Alguma parte deve ser revertida?** Não recomendo rollback amplo. Preservar a ideia de adapters A/B e as boundaries de fault. Reavaliar o gate de logs compartilhados e a cobertura dos dez serviços após comprovar o deploy.
- **Perto ou longe de uma bateria confiável?** Perto em fault injection, GT e isolamento de backends; ainda distante de atribuir resultados ao código congelado. Não iniciar bateria antes do gate de imagem.

## Resultado da auditoria

Nenhum código, configuração, teste ou estado do experimento foi alterado. Não foram executadas faults nem Chaos. Foram executados 103 testes Python e 10 testes Node: todos passaram. pytest, Go e .NET não estão disponíveis neste host; portanto os testes de runtime/build correspondentes não foram comprovados nesta auditoria.

Ver full-system-review.md, deployment-provenance-review.md e remediation-plan.md.

