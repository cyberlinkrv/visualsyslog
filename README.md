# VisualSyslog

Coletor Syslog multiplataforma, reimplementado em Python, com painel web local e armazenamento SQLite. O projeto original foi criado por Max Belkov e publicado sob GPL-2.0; este fork mantém essa licença. Consulte `NOTICE` para o histórico e a atribuição do projeto de origem.

## Recursos desta versão inicial

- Recebe mensagens por UDP e TCP; interpreta o PRI e campos comuns de RFC 3164 e RFC 5424.
- Painel responsivo em português, com atualização contínua, busca e filtro por severidade.
- Armazena eventos em SQLite, com índices para data, host e severidade. Não requer serviço externo de banco de dados.
- Retenção deslizante padrão de 1.095 dias (três anos), configurável no painel até 36.500 dias. A limpeza executa na inicialização e a cada hora. A expiração é baseada no instante de recebimento.
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

O painel abre em `http://127.0.0.1:8080`. O coletor escuta UDP e TCP na porta 5514. A porta 514 pode ser selecionada explicitamente, mas em Linux normalmente requer privilégios especiais; recomenda-se usar 5514 ou encaminhar a porta 514 pelo firewall/roteador.

```bash
visualsyslog --udp-port 5514 --tcp-port 5514 --web-port 8080
visualsyslog --syslog-host 0.0.0.0 --web-host 0.0.0.0 --no-browser
```

**Atenção:** publicar o painel em `0.0.0.0` expõe a interface na rede local e a versão inicial ainda não inclui autenticação. Use `127.0.0.1` (padrão), ou proteja a rede e adicione autenticação antes de expor em uma rede não confiável.

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
