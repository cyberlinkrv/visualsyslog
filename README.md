# VisualSyslog

Coletor Syslog multiplataforma, reimplementado em Python, com painel web local e armazenamento SQLite. O projeto original foi criado por Max Belkov e publicado sob GPL-2.0; este fork mantém essa licença. Consulte `NOTICE` para o histórico e a atribuição do projeto de origem.

## Recursos da versão 2.5.0

- Recebe mensagens por UDP e TCP; interpreta o PRI e campos comuns de RFC 3164 e RFC 5424.
- Painel responsivo em português e inglês, com atualização contínua e busca avançada em janela própria. Os termos sem aspas são combinados (AND), frases podem ser colocadas entre aspas e IPs/portas são comparados em limites exatos; os resultados são paginados.
- Armazena eventos em SQLite, com índices para data, host e severidade. Não requer serviço externo de banco de dados.
- Retenção deslizante padrão de 1.095 dias (três anos), configurável no painel até 36.500 dias. A limpeza executa na inicialização e a cada hora. A expiração é baseada no instante de recebimento.
- Configuração pela interface das portas Syslog UDP e TCP, com atualização imediata e persistência após reiniciar.
- Migração do banco para outro disco pela interface. O banco é copiado de forma consistente, o destino passa a ser usado e o arquivo anterior é removido após a migração; não é mantido backup.
- O banco contém timestamps e índices de data; os registros vencidos são removidos automaticamente. Não são gerados arquivos TXT nem um arquivo físico por dia.

## Executar a partir do código

Requer Python 3.10 ou superior.

```bash
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
visualsyslog
```

O painel abre em `http://127.0.0.1:8080`. O coletor escuta UDP e TCP na porta 5514, associado por padrão ao IP local que o sistema seleciona para a rota padrão. A porta 514 pode ser selecionada explicitamente, mas em Linux normalmente requer privilégios especiais; recomenda-se usar 5514 ou encaminhar a porta 514 pelo firewall/roteador.

```bash
visualsyslog --udp-port 5514 --tcp-port 5514 --web-port 8080
visualsyslog --syslog-host 0.0.0.0 --web-host 0.0.0.0 --no-browser
```

**Atenção:** publicar o painel em `0.0.0.0` expõe a interface na rede local e o aplicativo ainda não inclui autenticação. Use `127.0.0.1` (padrão), ou proteja a rede antes de expor em uma rede não confiável.

## Dados e retenção

Por padrão, o banco fica em `%LOCALAPPDATA%\VisualSyslog\visualsyslog.db` no Windows e em `~/.local/share/visualsyslog/visualsyslog.db` no Linux. `VISUALSYSLOG_DATA_DIR` define outra pasta, por exemplo para volume dedicado. Preserve essa pasta em backups; SQLite em modo WAL pode manter arquivos `-wal`/`-shm` enquanto o aplicativo estiver aberto.

Defina a retenção na interface. O prazo conta dias corridos a partir do recebimento; valores maiores preservam eventos por mais tempo. Três anos são aproximados por 1.095 dias.

## Build e instaladores

O workflow de release em `.github/workflows/build.yml` gera artefatos para Windows e Linux. No Windows, PyInstaller cria o executável e Inno Setup o empacota em instalador `.exe`. No Ubuntu, o build produz executável portátil e pacote `.deb`. Os artefatos são rascunhos de distribuição e devem ser validados em máquinas limpas de cada sistema antes de produção.

Para build local, instale o extra `.[build]` e execute `pyinstaller packaging/visualsyslog.spec`. Linux: `bash packaging/build_deb.sh`. O Inno Setup pode compilar `packaging/visualsyslog.iss` no Windows.

## Compatibilidade e escopo

Esta é uma primeira versão de modernização, não uma migração de configurações, mensagens ou funções avançadas do aplicativo legado. Alertas por e-mail, regras de ação, notificações, importação de logs históricos e serviço de sistema ainda não foram reimplementados. TCP trata uma linha por mensagem; framing octet-counted RFC 6587 e parsing completo de datas/estruturas RFC 5424 ainda precisam de trabalho. O formato SQLite é novo e independente dos arquivos históricos TXT.

## Desenvolvimento e testes

```bash
python -m pip install -e '.[test]'
pytest
```

Contribuições são bem-vindas. Mudanças redistribuídas devem seguir os termos da GPL-2.0.

## Configurações pela interface (versão 2.5)

No botão **Configurações**, é possível mudar o IP de escuta, as portas UDP e TCP, o prazo de retenção e o idioma (português do Brasil ou inglês dos Estados Unidos). No Windows, há também uma opção para iniciar o coletor ao entrar na conta, sem abrir automaticamente o navegador nem uma janela de prompt. Por padrão, o modo **Automático** identifica a cada inicialização o endereço IPv4 local escolhido pelo sistema para a rota padrão. No modo **Manual**, você pode escolher e persistir outro IP atribuído à máquina; `0.0.0.0` continua disponível caso queira escutar em todas as interfaces. Um IP inválido ou não atribuído é rejeitado sem substituir o listener atual. Se o IP manual salvo deixar de existir, o aplicativo inicia no IP padrão atual e exibe um aviso para corrigir a configuração. Para transferir o banco para outro disco, informe o caminho completo do novo arquivo, por exemplo `D:\\VisualSyslog\\visualsyslog.db`, e selecione **Mover banco**. O aplicativo copia o banco de forma consistente enquanto está aberto, ativa o arquivo no novo local e remove o arquivo anterior após a migração. Não mantém uma cópia de backup. O destino não pode existir previamente. Se o disco configurado ficar indisponível, os logs não são gravados em outro arquivo local; a interface mostra um aviso até que o banco seja configurado novamente. Mantenha o disco disponível e com espaço livre; se o caminho salvo não estiver acessível na próxima inicialização, o painel destaca o problema e mostra o caminho configurado para facilitar a correção. As configurações permanecem após reiniciar o aplicativo.
