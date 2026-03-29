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

### 2. Authorize Account

```bash
gog auth credentials ~/Downloads/client_secret_....json
gog auth add you@gmail.com
```

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