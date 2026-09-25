# NeuroClinic

Setup inicial do projeto — Semana 1 do cronograma do DVP.

## Passo a passo

### 1. Criar e ativar o ambiente virtual

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate
```

### 2. Instalar as dependências

```bash
pip install -r requirements.txt
```

### 3. Criar o banco no PostgreSQL

Com o PostgreSQL já instalado e rodando localmente:

```sql
CREATE DATABASE neuroclinic;
```

### 4. Configurar o `.env`

```bash
cp .env.example .env
```

Abra o `.env` e ajuste `DATABASE_URL` com o usuário/senha do seu PostgreSQL local,
e gere uma `SECRET_KEY` de verdade:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 5. Gerar e aplicar a primeira migration

```bash
alembic revision --autogenerate -m "cria tabela usuario"
alembic upgrade head
```

Isso cria a tabela `usuario` no banco a partir do model em `app/models/usuario.py`.

### 6. Rodar a aplicação

```bash
uvicorn app.main:app --reload
```

Acesse http://localhost:8000 — se aparecer "NeuroClinic está no ar", o setup está correto.

## Estrutura de pastas (Arquitetura em Camadas)

```
app/
  core/       → configuração (config.py), segurança (JWT, hash de senha)
  db/         → conexão com o banco (session.py) e Base declarativa (base.py)
  models/     → entidades SQLAlchemy (Model)
  schemas/    → validação de entrada/saída com Pydantic
  services/   → regras de negócio (RN), isoladas para testes automatizados
  routers/    → rotas HTTP, fazem o papel de Controller
  templates/  → páginas Jinja2 (View)
  static/     → CSS/JS customizados (Bootstrap vem via CDN)
alembic/      → migrations do banco de dados
tests/        → testes automatizados (Pytest)
```