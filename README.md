# FatoPontoEditor

Aplicativo local para organizar sinais de pauta recebidos por e-mail, priorizá-los com assistência de IA e preparar rascunhos jornalísticos para revisão humana.

## Sobre o projeto

O FatoPontoEditor apoia a rotina editorial do portal Fato & Ponto. Ele lê alertas não lidos de uma caixa IMAP, extrai links de notícias, agrupa sinais semelhantes e monta uma mesa de pauta local. A partir de uma pauta selecionada, o aplicativo busca conteúdo de fontes na web e gera um rascunho estruturado para conferência do editor.

O público principal são pessoas responsáveis por triagem e redação de pautas. A publicação é sempre manual: o aplicativo não envia conteúdo para um CMS nem publica matérias.

## Funcionalidades

- Leitura de mensagens não lidas em uma caixa IMAP e extração de links de alertas.
- Normalização de URLs, remoção de parâmetros de rastreamento e agrupamento de pautas semelhantes.
- Armazenamento local de sinais, pautas, fontes e rascunhos em SQLite.
- Triagem editorial em lotes com validação da resposta JSON.
- Uso do Codex CLI autenticado como provedor padrão de IA, com suporte opcional ao Ollama local.
- Busca de texto HTML em até três fontes por pauta, priorizando domínios oficiais.
- Interface gráfica em Tkinter para revisar pautas, apuração e rascunhos.
- Marcação de revisão e cópia do rascunho para publicação manual.

## Screenshots

Ainda não há screenshots públicos no repositório. Quando disponíveis, elas poderão ser adicionadas em uma pasta `screenshots/` e referenciadas aqui.

## Tecnologias utilizadas

- Python 3.13 (o ambiente atual do projeto usa 3.13.7).
- Tkinter para a interface gráfica local.
- SQLite, pela biblioteca padrão do Python, para armazenamento local.
- `requests` para acesso HTTP e `beautifulsoup4` para extração de conteúdo HTML.
- `python-dotenv` para carregar o arquivo `.env` local.
- Codex CLI como integração de IA padrão; Ollama pode ser usado como alternativa local.
- IMAP SSL, pela biblioteca padrão do Python, para a leitura de e-mails.

## Requisitos

- Windows 10 ou 11 com Python 3.13 recomendado. O projeto foi desenvolvido e verificado nesse ambiente; não há uma matriz formal para outros sistemas operacionais.
- Acesso a uma caixa IMAP SSL que receba os alertas de pauta.
- Para o provedor padrão, Codex CLI instalado e autenticado. Para o modo alternativo, uma instância local do Ollama em execução.
- Acesso à internet para ler e-mails e buscar as fontes das pautas.

## Instalação

```bash
git clone https://github.com/SEU-USUARIO/FatoPontoEditor.git
cd FatoPontoEditor
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Abra `.env` em um editor e preencha as credenciais da caixa IMAP. Esse arquivo é local e nunca deve ser versionado.

## Configuração

O arquivo `.env` é carregado a partir da raiz do projeto.

| Variável | Obrigatória | Finalidade |
| --- | --- | --- |
| `MAIL_HOST` | Sim | Servidor IMAP SSL da caixa de alertas. |
| `MAIL_USER` | Sim | Usuário da caixa IMAP. |
| `MAIL_PASSWORD` | Sim | Senha ou senha de aplicativo da caixa IMAP. |
| `MAIL_PORT` | Não | Porta IMAP; quando omitida, o aplicativo usa 993. |
| `MAIL_FOLDER` | Não | Pasta IMAP; quando omitida, usa `INBOX`. |
| `AI_PROVIDER` | Não | Seleciona o provedor de IA. Sem configuração, o aplicativo usa Codex. |
| `CODEX_COMMAND` | Não | Comando do Codex CLI quando ele não estiver disponível como `codex` no `PATH`. |
| `CODEX_TRIAGE_MODEL` e `CODEX_EDITOR_MODEL` | Não | Modelos usados na triagem e na geração de rascunhos. |
| `CODEX_STRICT_SCHEMA` | Não | Ativa a passagem de schema para o Codex CLI. |
| `ALLOW_OLLAMA_FALLBACK` | Não | Permite recorrer ao Ollama se o Codex falhar. |
| `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_TRIAGE_MODEL`, `OLLAMA_EDITOR_MODEL` e `OLLAMA_THREADS` | Não | Configuram o provedor Ollama local. |

Use valores próprios somente no `.env`; não os coloque em código, documentação, Issues ou Pull Requests.

## Como usar

1. Instale as dependências e configure a caixa IMAP conforme as seções anteriores.
2. Se estiver usando Codex, confirme que a CLI está instalada e autenticada. Se estiver usando Ollama, inicie o serviço local e configure o provedor no `.env`.
3. Inicie o aplicativo:

   ```bash
   python app.py
   ```

4. Na janela, selecione **Atualizar e-mails** para importar os links de alertas não lidos.
5. Selecione **Rodar triagem** para agrupar e classificar os sinais. As pautas mantidas aparecem na mesa de pauta.
6. Escolha uma pauta, abra a primeira fonte quando necessário e use **Buscar fontes** para preencher a aba de apuração. Revise e complemente esse material antes de continuar.
7. Selecione **Gerar rascunho**, revise o texto e as pendências de confirmação, depois copie o rascunho ou marque a pauta como revisada.
8. Publique manualmente no destino editorial escolhido, após a revisão humana.

O projeto também mantém dois scripts de linha de comando voltados ao fluxo com Ollama: `triagem.py` para triagem e `editor.py` para análise manual de uma pauta. O script `reprocessar_pendentes.py` cria um backup local e recoloca para triagem as pautas classificadas como `VALIDACAO_PENDENTE`.

## Desenvolvimento

Ative o ambiente virtual, instale as dependências e execute `python app.py`. Os dados de desenvolvimento são criados automaticamente em `data/`, que é ignorada pelo Git porque pode conter conteúdo editorial e dados de usuários.

Antes de enviar mudanças, valide a sintaxe dos módulos:

```bash
python -m compileall -q app.py editor.py triagem.py reprocessar_pendentes.py services
```

## Build

O projeto não possui, atualmente, uma configuração de empacotamento ou geração de instalador. A distribuição suportada é a execução a partir do código-fonte com Python e as dependências listadas em `requirements.txt`.

## Estrutura do projeto

```text
FatoPontoEditor/
├── app.py                     # Aplicativo gráfico principal e fluxo editorial
├── editor.py                  # Analisador de pauta via linha de comando
├── triagem.py                 # Triagem de alertas via linha de comando
├── reprocessar_pendentes.py   # Recoloca pautas pendentes na fila local
├── services/
│   ├── codex_client.py        # Integração com o Codex CLI
│   └── email_reader.py        # Leitura IMAP e extração de links
├── prompts/                   # Instruções editoriais para a IA
├── schemas/                   # Schemas JSON esperados nas respostas da IA
├── requirements.txt           # Dependências Python
└── .env.example               # Modelo sem valores de configuração local
```

## Segurança

Não reporte vulnerabilidades em Issues públicas. Consulte [SECURITY.md](SECURITY.md) antes de compartilhar um relato de segurança.

## Contribuindo

Consulte [CONTRIBUTING.md](CONTRIBUTING.md) para o fluxo de contribuição.

## Licença

Este projeto é distribuído sob a [MIT License](LICENSE).
