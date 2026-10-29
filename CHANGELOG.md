# Changelog

## 2.6.2 — 2026-10-29

### Português (Brasil)

- Resultados da busca em páginas de até 100 linhas, com navegação por indicador de mais resultados.
- Busca rápida deixa de calcular o total exato de todos os acertos antes de exibir a página; a ordenação usa o rowid descendente do FTS5 para evitar ordenar toda a lista temporariamente.
- Atualização automática do fluxo também evita varredura de contagem. O painel usa o contador incremental e a busca continua compatível com o filtro de data.
- API preserva respostas completas por padrão; o painel solicita o modo rápido e omite o payload `raw` redundante.
- Testes cobrem paginação, filtro diário, ausência de contagem global e consultas sem ordenação temporária.

### English

- Search results are shown in pages of up to 100 rows, with a continuation indicator.
- Fast search no longer counts every match before displaying a page; descending FTS5 rowids avoid sorting the full result set into a temporary structure.
- Automatic dashboard refresh also avoids a full count scan. The dashboard uses incremental counters, and search retains the existing date filter.
- The API keeps its complete-response behavior by default; the dashboard requests fast mode and omits the redundant `raw` payload.
- Tests cover pagination, day filtering, no global count, and no temporary sort.

## 2.6.1 — 2026-10-28

### Português (Brasil)

- A busca aceita um dia específico como filtro opcional; sem data, mantém a pesquisa em todo o período disponível.
- Otimização para bancos grandes: painel usa contadores incrementais e a busca evita trabalho redundante de regex para termos simples.
- Índice FTS5 compacto, baseado no payload Syslog bruto e no IP de origem, mantendo pesquisa por frases entre aspas.
- Novos payloads `raw` são comprimidos quando isso reduz o espaço armazenado; o conteúdo continua legível pela API.
- Atualizações e reinicializações durante a reconstrução do índice são retomáveis; gatilhos e cursor protegem a consistência mesmo com mais de uma instância.
- A interface deixa de transferir o campo `raw` em cada atualização; a API mantém `raw` por padrão e aceita `include_raw=false`.
- Rodapé alinhado à esquerda. O filtro de data e as otimizações de banco são cobertos por testes automatizados.

#### Migração e armazenamento

- Nenhuma mensagem existente é apagada pela atualização. O índice FTS anterior é reconstruído em lotes; durante o processo, coleta e painel continuam disponíveis, mas buscas textuais aguardam a conclusão.
- Registros antigos permanecem preservados; a compressão é aplicada a payloads novos. SQLite pode reutilizar páginas liberadas sem reduzir imediatamente o tamanho físico do arquivo `.db`; `VACUUM` é uma operação separada que requer backup e espaço livre adicional.

### English

- Search accepts a specific day as an optional filter; omitting the date searches the full available period.
- Large-database optimizations: the dashboard uses incremental counters, and simple search terms avoid redundant regex work.
- Compact FTS5 index based on the original Syslog payload and source IP, while retaining quoted-phrase search.
- New `raw` payloads are compressed when doing so reduces stored size; the API continues to return readable text.
- Index rebuilds can resume after restarts; cursor-aware triggers preserve consistency during updates and when multiple instances share a database.
- The UI no longer transfers `raw` on each refresh. API clients still receive it by default and can use `include_raw=false`.
- Footer aligned to the left. Optional date filtering and database optimizations are covered by automated tests.

#### Migration and storage

- Existing messages are not deleted by the upgrade. The previous FTS index is rebuilt in batches; collection and the dashboard remain available, while text searches wait for indexing to finish.
- Older records remain preserved; compression applies to new payloads. SQLite may reuse freed pages without immediately shrinking the physical `.db` file. `VACUUM` is separate and requires a backup and additional free disk space.

## 2.6.0

See the [2.6.0 GitHub release](https://github.com/cyberlinkrv/visualsyslog/releases/tag/v2.6.0) for the previous release notes and downloads.
