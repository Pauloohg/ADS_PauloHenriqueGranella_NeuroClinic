# NeuroClinic

Plataforma web para gestão de atendimentos de uma clínica de psicopedagogia, organizada em
planos recorrentes de sessões. Projeto acadêmico (UPF — ADS/CCC/ECP).

**Perfis:** Cliente (responsável pelo paciente), Funcionário (profissional) e Admin.

**Stack:** Python 3.12, FastAPI, Jinja2 + Bootstrap 5.3, PostgreSQL 16, SQLAlchemy 2.0, Alembic,
PyJWT, Passlib (bcrypt), fastapi-mail, Pytest.

## Status

| Semana | Entrega | Situação |
|---|---|---|
| 1 | Setup, banco e RF01 (cadastro, login, redefinição de senha) | Concluída |
| 2 | RF02 (perfil), RF03 (pacientes), RF11 (funcionários), RF12 (especialidades) | Concluída |
| 3 em diante | Planos, pagamento, remarcação, laudos e histórico | Pendente |

## Passo a passo

### 1. Criar e ativar o ambiente virtual

Requer Python 3.12.

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

Atenção às versões fixadas no `requirements.txt`, não atualize sem testar:

- `bcrypt==4.0.1`: versões 4.1 ou superiores são incompatíveis com o Passlib 1.7.4 e quebram o
  hash de senha.
- `tzdata`: necessário no Windows, que não traz a base de fusos horários. Sem ele, o cálculo do
  horário de Brasília (`America/Sao_Paulo`) falha.

### 3. Criar o banco no PostgreSQL

Com o PostgreSQL já instalado e rodando localmente:

```sql
CREATE DATABASE neuroclinic;
```

### 4. Configurar o `.env`

```bash
cp .env.example .env
```

Abra o `.env` e ajuste:

- `DATABASE_URL`: usuário e senha do seu PostgreSQL local.
- `SECRET_KEY`: gere uma de verdade:

```bash
  python -c "import secrets; print(secrets.token_hex(32))"
```

- `MAIL_*`: servidor SMTP usado para o e-mail de redefinição de senha. Em desenvolvimento,
  use o Email Sandbox do Mailtrap (gratuito), que intercepta os e-mails sem entregá-los a
  ninguém. Sem essas variáveis, a aplicação funciona, mas nenhum e-mail é enviado (a falha de
  SMTP é apenas registrada no log).

### 5. Criar o banco a partir das migrations

```bash
alembic upgrade head
```

Isso cria todas as tabelas e enums do banco a partir da migration inicial, em um PostgreSQL
vazio. **Não rode `alembic revision --autogenerate` ao configurar o projeto:** a migration
inicial já existe no repositório.

Ao alterar um model, gere uma nova migration e **abra o arquivo gerado antes de aplicar**,
conferindo que não há `op.drop_table(...)` inesperado (acontece quando um model novo não é
importado em `app/models/__init__.py`):

```bash
alembic revision --autogenerate -m "descrição da mudança"
alembic upgrade head
```

### 6. Criar o primeiro Administrador

Não existe autocadastro de Admin pela tela (o `/cadastro` cria só Clientes). Com o banco já
migrado, rode na raiz do projeto, com o ambiente virtual ativo:

```bash
python -m scripts.criar_admin
```

O script pede nome, e-mail e senha no terminal (a senha não aparece ao digitar e é pedida duas
vezes), valida com as mesmas regras do cadastro (e-mail válido e único, senha com pelo menos 8
caracteres) e grava o usuário com `tipo=admin` e a senha em hash. Depois é só entrar pelo
`/login`. Funcionários são cadastrados pelo próprio Admin, na tela **Funcionários**, e as
especialidades e os valores de sessão, na tela **Especialidades**.

### 7. Rodar a aplicação

```bash
uvicorn app.main:app --reload
```

Acesse http://localhost:8000. Deve aparecer a página inicial da NeuroClinic.

### 8. Rodar os testes

```bash
pytest
```

Os testes usam um banco temporário (SQLite em memória), então não tocam no PostgreSQL, e o envio
de e-mail é bloqueado. As regras de negócio recebem a data e a hora por parâmetro, o que permite
testá-las com horários fixos, sem depender do relógio real.

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
scripts/      → scripts de linha de comando (ex.: criar_admin.py)
tests/        → testes automatizados (Pytest)
```
