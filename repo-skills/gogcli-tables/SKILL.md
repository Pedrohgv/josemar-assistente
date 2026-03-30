---
name: gogcli-tables
description: Google Workspace CLI with Sheets Table manipulation support. Custom build from Pedrohgv/gogcli fork (feat/sheets-table-manipulation branch). Use when user needs to interact with Gmail, Calendar, Drive, Sheets (including Tables API), Docs, Slides, Contacts, Tasks, or other Google Workspace services. Triggers on mentions of Google services, Sheets tables, or gog/gogcli commands.
---

# gogcli-tables

Custom build of gogcli with Sheets Table manipulation support (list, get, create, update, append, clear, delete tables). Built from `Pedrohgv/gogcli` fork, branch `feat/sheets-table-manipulation`.

## ⚠️ IMPORTANTE: Aprovação Obrigatória

**ANTES de executar qualquer ação destrutiva ou de escrita, SEMPRE confirme com o usuário:**

- ✋ **Calendar:** Criar, editar ou deletar eventos → **PERGUNTAR PRIMEIRO**
- ✋ **Gmail:** Enviar emails, deletar mensagens → **PERGUNTAR PRIMEIRO**
- ✋ **Drive:** Deletar arquivos, modificar permissões → **PERGUNTAR PRIMEIRO**
- ✋ **Sheets:** Criar/deletar tabelas, modificar dados → **PERGUNTAR PRIMEIRO**

Ações de **leitura** (listar, buscar, exportar) podem ser executadas livremente.

**Exemplo:**
> "Vou criar o evento 'Reunião Teste' amanhã às 5h. Posso prosseguir?"

## Installation

gogcli is automatically built and installed in the Docker image at build time from the Pedrohgv/gogcli fork (branch: `feat/sheets-table-manipulation`).

**Binary location:** `/usr/local/bin/gog`

**Verify installation:**
```bash
gog --version
```

Or run the verification script:
```bash
/root/.openclaw/skills/gogcli-tables/scripts/install.sh
```

## First-Time Setup

### 1. Create OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create project or use existing
3. Configure OAuth consent screen
4. Create OAuth 2.0 client (Desktop app)
5. Download JSON credentials

**Note:** If using Josemar Assistente with OpenClaw, OAuth client credentials are typically already available at:
```
/root/.openclaw/credentials/gogcli/client_secret_*.json
```

If this file exists, you can skip steps 1-5 above and proceed directly to authorization.

### 2. Authorize Account

**IMPORTANT - OAuth Flow Instructions:**

When authenticating, the process requires manual intervention:

1. The agent will provide you with a Google OAuth URL
2. **Click the URL and complete the Google authentication** in your browser
3. **You will be redirected to a localhost URL** (e.g., `http://localhost/?code=...&scope=...`)
4. **This localhost page will show "cannot be reached" - THIS IS EXPECTED**
5. **Copy the full URL from your browser's address bar** (the entire localhost URL with all parameters)
6. **Paste the URL back into the chat**
7. The agent will complete the authentication process

**Example:**
> After clicking the authorization link and approving access, you were redirected to:
> `http://localhost/?code=4/0AVG7fiQp...&scope=https://www.googleapis.com/auth/gmail.readonly`
>
> Please paste this callback URL into the chat so I can complete the authentication.

**Commands:**
```bash
gog auth credentials /root/.openclaw/credentials/gogcli/client_secret_*.json
gog auth add you@gmail.com
```

### 2b. Headless/Container Environment (Remote OAuth)

**Use this method when gogcli runs in a container or remote server** (like inside OpenClaw).

The standard OAuth flow doesn't work in containers because the callback goes to `localhost` on your browser, not the server. Use the `--remote` flag instead:

**Step 1 - Generate the authorization URL:**
```bash
gog auth add you@gmail.com --remote --step 1
```

This will output a URL like:
```
https://accounts.google.com/o/oauth2/auth?client_id=...&redirect_uri=http://127.0.0.1:8085/oauth2/callback&...
```

**Step 2 - Open the URL in your browser:**
1. Click the URL or copy it to your browser
2. Complete the Google authentication
3. You will be redirected to `http://127.0.0.1:8085/oauth2/callback?code=...&scope=...`
4. **This will show "This site can't be reached" or 404 - THIS IS EXPECTED**
5. **Copy the FULL URL from your browser's address bar** (including the `code=` parameter)

**Step 3 - Complete authentication with the callback URL:**
```bash
gog auth add you@gmail.com --remote --step 2 --auth-url "paste_the_full_callback_url_here"
```

**Why this works:**
- `--step 1` generates the auth URL without starting a local server
- The callback goes to your browser's localhost (which doesn't exist)
- `--step 2` completes the auth by manually providing the callback URL with the authorization code
- This bypasses the need for the server to receive the callback directly

**Note:** OAuth tokens are now persisted in `/root/.openclaw/workspace/.config/gogcli/` (set via `XDG_CONFIG_HOME`) and survive container restarts.

### 3. Verify

```bash
gog auth list
gog gmail search 'is:unread' --max 5
```

## Common Commands

### Gmail

```bash
gog gmail search 'query' --max 20
gog gmail send --to recipient@example.com --subject 'Hello' --body 'Message'
gog gmail labels list
```

### Calendar

```bash
gog calendar events --today
gog calendar create primary --summary 'Meeting' --from 2025-01-15T10:00:00Z --to 2025-01-15T11:00:00Z
```

### Drive

```bash
gog drive ls --max 20
gog drive upload ./file.pdf --parent <folderId>
gog drive download <fileId> --out ./downloaded.bin
```

### Sheets

```bash
gog sheets list
gog sheets export <spreadsheet-id> --format csv --out ./sheet.csv
```

### Sheets Tables (Custom Feature)

These commands are from the custom fork and support Google Sheets Tables API:

```bash
# List all tables in a spreadsheet
gog sheets table list <spreadsheet-id>

# Get table details
gog sheets table get <spreadsheet-id> <table-id>

# Create a new table
gog sheets table create <spreadsheet-id> <range> --name 'MyTable' --columns-json '[{"name":"Col1","type":"TEXT"},{"name":"Col2","type":"NUMBER"}]'

# Update table properties
gog sheets table update <spreadsheet-id> <table-id> --name 'RenamedTable'

# Append rows to table (respects footer)
gog sheets table append <spreadsheet-id> <table-id> '["val1", "val2"]' '["val3", "val4"]'

# Clear table contents
gog sheets table clear <spreadsheet-id> <table-id>

# Delete table
gog sheets table delete <spreadsheet-id> <table-id>
```

### Contacts

```bash
gog contacts search 'John Doe'
```

### Tasks

```bash
gog tasks list
gog tasks add --title 'Task' --due '2025-01-30'
```

## Notes

- Use `--json` flag for scripting/automation
- Use `gog --help` to explore all commands
- Credentials stored securely in system keyring
- Run `gog auth list --check` to verify token validity