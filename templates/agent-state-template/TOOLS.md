# TOOLS.md

Notas de capacidades core para um state repo novo.

## Escopo de Skills

- Skills core embarcadas devem ser tratadas apenas pelo que existe em runtime.
- Skills especificas do usuario devem ficar somente neste state repo, em `skills/`.

## Skills Core Embarcadas (Bundled)

### vault-gateway

- Caminho em runtime: `/opt/josemar/skills/vault-gateway/`
- Finalidade: ponto unico de entrada para operacoes no Obsidian vault
- Uso: seguir contrato estrito `route` + `payload`

### aux-ml

- Caminho em runtime: `/opt/josemar/skills/aux-ml/`
- Finalidade: envio e acompanhamento de jobs longos (OCR/transcricao) via fila
- Regra: qualquer tarefa relacionada a OCR deve ser roteada para `aux-ml`

### workspace-sync

- Caminho em runtime: `/opt/josemar/skills/workspace-sync/`
- Finalidade: operacoes git do workspace (`status`, `diff`, `log`, `commit`, `push`, `pull`, `sync`)

## Integracao Auxiliar de ML

O servico auxiliar de ML e opcional e depende de profile.

Habilite no `.env`:

```bash
AUX_ML_ENABLED=true
COMPOSE_PROFILES=aux-ml
```

Depois inicie ou reinicie os servicos:

```bash
docker compose up -d --build
```

## Checagens de Runtime

Antes de usar uma capacidade, valide no ambiente ativo:

- Confirmar que a skill embarcada esta presente em `/opt/josemar/skills/`
- Confirmar servicos opcionais (como `aux-ml`) quando necessario
- Se uma capacidade estiver ausente, informar claramente e seguir com os caminhos disponiveis
