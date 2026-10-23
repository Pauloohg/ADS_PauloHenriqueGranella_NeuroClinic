# NeuroClinic

Plataforma web para gestão de atendimentos de uma clínica de psicopedagogia, organizada em
planos recorrentes de sessões. Projeto acadêmico (UPF — ADS/CCC/ECP).

O acompanhamento psicopedagógico é recorrente: a mesma criança é atendida pelo mesmo profissional,
em dia e horário fixos, durante semanas ou meses. Por isso o sistema não trabalha com reservas
avulsas: o responsável contrata um **plano** uma única vez e o sistema o desdobra em várias
**sessões**.

**Perfis:** Cliente (responsável pelo paciente), Funcionário (profissional) e Admin (administração
da clínica).

**Stack:** Python 3.12, FastAPI, Jinja2 + Bootstrap 5.3, PostgreSQL 16, SQLAlchemy 2.0, Alembic,
PyJWT, Passlib (bcrypt), fastapi-mail, AbacatePay API v2 (pagamentos), `holidays` (feriados
nacionais), Pytest.

## Funcionalidades

**Cliente**
- Cadastro, login e redefinição de senha por e-mail (link válido por 30 minutos, uso único).
- Perfil (nome, e-mail, telefone, CPF opcional) e alteração de senha.
- Cadastro de um ou mais pacientes.
- Consulta de especialidades e dos profissionais ativos de cada uma.
- Contratação e renovação de plano: escolha de especialidade, profissional, paciente, dia(s) e
  horário, frequência semanal e duração. O sistema simula as sessões, valida a disponibilidade, pula
  feriados nacionais e calcula o valor pela quantidade real de sessões.
- Pagamento do plano por Pix (pagar em até 4 horas) ou boleto (vencimento próprio em dias úteis,
  respeitando a data da primeira sessão), via AbacatePay, com confirmação automática por webhook,
  conferência de reserva na própria AbacatePay e e-mail de confirmação.
- Página inicial com a próxima sessão e os planos (ativo, aguardando pagamento, encerrado).

**Funcionário**
- Login, página inicial e alteração de senha.

**Admin**
- Gestão de funcionários (cadastro, edição, desativação e reativação) e de especialidades e valores
  de sessão.
- Consulta dos clientes cadastrados, com seus pacientes e planos.
- Painel gerencial: receita do mês, planos contratados, novos clientes, ranking de funcionários e
  desempenho por especialidade.

## Status

| Semana | Entrega | Situação |
|---|---|---|
| 1 | Setup, banco e RF01 (cadastro, login, redefinição de senha) | Concluída |
| 2 | RF02 (perfil), RF03 (pacientes), RF11 (funcionários), RF12 (especialidades) | Concluída |
| 3 | RF04 (especialidades e profissionais) e RF05 (simulação e disponibilidade, feriados) | Concluída |
| 4 | RF05 (confirmação e renovação) e RF06 (pagamento via AbacatePay) | Concluída |
| 5 | RF08 (agenda do funcionário) e RF07 (remarcação) | Pendente |
| 6 | RF09 (laudos) e RF10 (histórico) | Pendente |
| 7 e 8 | Testes, usabilidade, segurança e apresentação | Pendente |

Também já fazem parte do projeto o RF14 (consulta de clientes pelo Admin) e o RF15 (painel
gerencial). O RF13 (lembrete por WhatsApp) é opcional e só será feito se sobrar tempo.

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

| Variável | O que é |
|---|---|
| `DATABASE_URL` | Usuário e senha do seu PostgreSQL local. |
| `SECRET_KEY` | Chave dos tokens JWT. Gere uma de verdade: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `MAIL_*` | Servidor SMTP dos e-mails (redefinição de senha e confirmação de pagamento). Em desenvolvimento, use o Email Sandbox do Mailtrap (gratuito), que intercepta os e-mails sem entregá-los a ninguém. Sem essas variáveis a aplicação funciona, mas nenhum e-mail é enviado (a falha de SMTP é apenas registrada no log). |
| `ABACATEPAY_API_KEY` | Chave de API da AbacatePay em Dev mode (ver a seção de pagamento abaixo). |
| `ABACATEPAY_BASE_URL` | `https://api.abacatepay.com/v2` |
| `ABACATEPAY_WEBHOOK_SECRET` | Segredo do webhook, o mesmo valor cadastrado no painel da AbacatePay. |

Não deixe espaço depois do `=` e reinicie o servidor sempre que alterar o `.env`.

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

Os testes usam um banco temporário (SQLite em memória), então não tocam no PostgreSQL. O envio de
e-mail e qualquer chamada de rede são bloqueados, e a AbacatePay é substituída por uma versão
falsa. As regras de negócio recebem a data e a hora por parâmetro, o que permite testá-las com
horários fixos, sem depender do relógio real.

## Pagamento em desenvolvimento (AbacatePay, Dev mode)

O sistema cobra por Pix ou boleto pela API v2 da AbacatePay (`/v2/transparents/create`). O caminho
principal de confirmação é o **webhook** (evento `transparent.completed`): quando a AbacatePay
avisa, as sessões do plano passam de `pendente_pagamento` para `agendada`, o plano fica ativo e o
e-mail de confirmação é enviado. Como o servidor roda em `localhost`, o webhook precisa de um
túnel público.

Como rede de segurança, a página de cobrança também **confere o pagamento na API** da AbacatePay
(`GET /v2/transparents/list`, no máximo uma vez a cada 10 segundos por cobrança): se a cobrança
estiver paga e o webhook não tiver chegado, o sistema confirma pelo mesmo caminho do webhook, sem
duplicar a confirmação nem o e-mail. Isso só acontece enquanto a página de cobrança está aberta;
com o webhook funcionando, o plano é confirmado mesmo com o navegador fechado.

### 1. Conta e chave de API

1. Crie uma conta na AbacatePay e ative o **Modo Teste (Sandbox)**.
2. Em API Keys, crie uma chave **API v2** com escopo **Personalizado** e as permissões
   `TRANSPARENT:CREATE` e `TRANSPARENT:READ`. As permissões não podem ser editadas depois.
3. Coloque a chave em `ABACATEPAY_API_KEY` no `.env`. Chaves de Dev mode começam com `abc_dev_`.

### 2. Túnel e webhook

1. Crie uma conta gratuita no [ngrok](https://ngrok.com), configure o token (`ngrok config
   add-authtoken ...`) e suba o túnel com o seu domínio fixo gratuito:

   ```bash
   ngrok http --url=SEU-DOMINIO.ngrok-free.dev 8000
   ```

2. No painel da AbacatePay (Dev mode), em **Integração → Webhooks → Criar webhook** (versão v2):
   - URL: `https://SEU-DOMINIO.ngrok-free.dev/webhooks/abacatepay`, **sem** `?webhookSecret`
     (o painel acrescenta o segredo sozinho).
   - Secret: o mesmo valor de `ABACATEPAY_WEBHOOK_SECRET` do `.env`.
   - Evento: "Pagamento transparente concluído".

O servidor recusa com 401 qualquer chamada cujo segredo não confira. Para acompanhar as chamadas
recebidas, abra `http://127.0.0.1:4040` (painel local do ngrok).

### 3. Simular o pagamento

No Dev mode ninguém paga de verdade. Há dois scripts, com papéis diferentes:

**Pix: `simular_pagamento`** marca a cobrança como paga na própria AbacatePay (a simulação deles
só existe para Pix):

```bash
python -m scripts.simular_pagamento <id do plano>
```

Com o ngrok no ar, a AbacatePay avisa o webhook. Sem o ngrok, deixe a página de cobrança aberta:
em poucos segundos a conferência descobre o pagamento e confirma. Em ambos os casos a página vira
"Pagamento confirmado", o plano passa a "Ativo" e o e-mail chega ao Mailtrap.

**Pix ou boleto: `simular_webhook`** não fala com a AbacatePay: monta o aviso de pagamento, assina
como a AbacatePay assina e envia para o servidor local (não precisa de ngrok):

```bash
python -m scripts.simular_webhook <id do plano>
```

É o único jeito de ver um boleto ser confirmado em teste. Os dois scripts se recusam a rodar fora
do Dev mode (chave `abc_dev_`), e o `simular_webhook` só envia para `localhost`.

### Limitações conhecidas

- A simulação do Dev mode só existe para **Pix**. O boleto é criado de verdade pela AbacatePay
  (linha digitável, vencimento e PDF), mas não há como pagá-lo em teste: sua confirmação foi
  validada apenas com o aviso simulado pelo `simular_webhook`, que prova o nosso lado e não o
  formato real do evento do boleto.
- Prazos (RN09): o Pix deve ser pago em até 4 horas. O boleto vence em até 3 dias úteis, nunca
  depois do 2º dia útil anterior à primeira sessão; se isso cair hoje ou antes, só o Pix é
  oferecido. Pagamentos fora do prazo só são aceitos se a reserva ainda existir e o horário
  continuar livre.
- Pagamentos que chegam para um plano já removido ou com horário ocupado não são confirmados: o
  caso é registrado no log para estorno manual.
- O ngrok e o servidor precisam estar rodando para o webhook funcionar. Antes de uma demonstração,
  suba os dois e faça um pagamento de teste; se o webhook falhar, a conferência cobre o Pix com a
  página de cobrança aberta.

## Regras de negócio

| Regra | Resumo |
|---|---|
| RN01 | Um funcionário não pode ter duas sessões com períodos sobrepostos. |
| RN02 | A remarcação exige no mínimo 4 horas de antecedência. |
| RN03 | O laudo só é acessível ao cliente responsável, ao Admin e a funcionário com sessão vinculada ao paciente. |
| RN04 | O laudo só pode ser liberado depois de a sessão estar realizada. |
| RN05 | Funcionário só pode ser desativado sem sessões futuras agendadas ou aguardando pagamento. |
| RN06 | Feriados nacionais não têm atendimento: a sessão é deslocada para a semana seguinte, sem mudar a quantidade de sessões nem o valor. |
| RN07 | A especialidade de um funcionário só pode mudar nas mesmas condições da RN05. |
| RN08 | Sessões de 60 minutos, em hora cheia, de segunda a sexta, das 8h às 11h e das 14h às 17h. |
| RN09 | O plano reservado deve ser pago em até 4 horas (Pix) ou até o vencimento do boleto (menor entre 3 dias úteis e o 2º dia útil antes da primeira sessão). Depois do prazo, as sessões pendentes deixam de bloquear o horário e o plano é removido após a carência (60 minutos no Pix, 1 dia útil no boleto), nunca se houver sessão agendada, realizada ou pagamento confirmado. |
| RN10 | A primeira sessão de um plano precisa estar a pelo menos 4 horas de distância. |

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
scripts/      → scripts de linha de comando (criar_admin, simular_pagamento, simular_webhook)
tests/        → testes automatizados (Pytest)
```

## Segurança

- O `.env` e o `.venv` não são versionados. Nunca publique chaves de API, segredos nem credenciais
  de e-mail em commits, prints ou conversas.
- Se uma chave for exposta, gere outra no painel do serviço e atualize o `.env`.