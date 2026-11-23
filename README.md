# NeuroClinic

Plataforma web para gestão de atendimentos de uma clínica de psicopedagogia, organizada em
planos recorrentes de sessões. Projeto acadêmico (UPF — ADS/CCC/ECP).

O acompanhamento psicopedagógico é recorrente: a mesma criança é atendida pelo mesmo profissional,
em dia e horário fixos, durante semanas ou meses. Por isso o sistema não trabalha com reservas
avulsas: o responsável contrata um **plano** uma única vez e o sistema o desdobra em várias
**sessões**.

**Perfis:** Cliente (responsável pelo atendido), Profissional (quem realiza os atendimentos) e Admin
(administração da clínica).

**Vocabulário:** na interface, quem é atendido é o **atendido**, quem atende é o **profissional** e o
documento de cada sessão é a **avaliação**. No código e no banco, esses nomes continuam `Paciente`,
`Funcionario` e `Avaliacao` (tabelas `paciente`, `funcionario` e `avaliacao`).

**Stack:** Python 3.12, FastAPI, Jinja2 + Bootstrap 5.3, PostgreSQL 16, SQLAlchemy 2.0, Alembic,
PyJWT, Passlib (bcrypt), fastapi-mail, AbacatePay API v2 (pagamentos), `holidays` (feriados
nacionais), Pytest.

## Funcionalidades

**Cliente**
- Cadastro, login e redefinição de senha por e-mail (link válido por 30 minutos, uso único).
- Página inicial com o bloco "Primeiros passos" (cadastrar o atendido, contratar um plano, pagar,
  acompanhar as sessões), a próxima sessão e os planos (ativo, aguardando pagamento, encerrado).
- Perfil (nome, e-mail, telefone, CPF opcional) e alteração de senha. O cadastro e o perfil avisam que o nome e o
  telefone ficam visíveis para os profissionais que atendem a pessoa cadastrada.
- Cadastro de um ou mais atendidos.
- Consulta de especialidades e dos profissionais ativos de cada uma, com formação, registro
  profissional, apresentação e contato quando o Admin ou o próprio profissional os preencheu.
- Contratação e renovação de plano: escolha de especialidade, profissional, atendido, dia(s) e
  horário, frequência semanal e duração. O sistema simula as sessões, valida a disponibilidade, pula
  feriados nacionais e calcula o valor pela quantidade real de sessões, com desconto conforme a
  duração do plano.
- Pagamento do plano por Pix (pagar em até 4 horas) ou boleto (vencimento próprio em dias úteis,
  respeitando a data da primeira sessão), via AbacatePay, com confirmação automática por webhook,
  conferência de reserva na própria AbacatePay e e-mail de confirmação.
- Lista de consultas (próximas e anteriores) e remarcação de uma consulta já paga: o Cliente escolhe uma
  nova data e um horário livre do mesmo profissional, com pelo menos 4 horas de antecedência do horário
  original, sem cair em feriado nem fora dos horários da clínica; as demais sessões do plano não mudam.
- Histórico de cada atendido (um por vez): todas as consultas, da mais recente para a mais antiga, com o
  status de cada uma, o aviso de não comparecimento e a avaliação em PDF quando o profissional a envia
  (o PDF abre no navegador; só o responsável pelo atendido, o Admin e profissionais com consulta do
  atendido conseguem acessá-lo). Um e-mail avisa quando há avaliação nova, sem anexar o arquivo.

**Profissional**
- Login, página inicial (com a quantidade de consultas de hoje) e alteração de senha.
- Meu perfil: o profissional edita a formação, o registro profissional, a apresentação e os contatos (telefone e e-mail
  de contato) que o Cliente vê ao escolher o profissional. Nome, e-mail de acesso, especialidade e situação continuam
  com o Admin; se os dois editarem os mesmos campos, vale a última gravação.
- Agenda de atendimentos por dia, semana ou período (até 92 dias), com o atendido (nome e idade), a
  especialidade e a observação do plano; só aparecem consultas já confirmadas (pagas).
- Registro do atendimento depois que a sessão termina: o profissional anexa a avaliação em PDF (até
  10 MB) ou marca que o atendido não compareceu. O registro é definitivo, por isso há uma confirmação antes de gravar (um checkbox no envio do PDF e uma tela de
  confirmação no não comparecimento). A aba "A registrar" da
  agenda lista as consultas que esperam esse registro.
- Meus atendidos: lista dos atendidos com consultas confirmadas com o profissional e, para cada um, o acompanhamento
  (planos com progresso, consultas, avaliações que ele enviou). Mostra só o que é dele, sem valores nem pagamento. No
  detalhe do atendido aparecem o nome e o telefone do responsável, para contato; o e-mail e o CPF nunca aparecem, e o
  contato não aparece na lista nem na agenda.

**Admin**
- Gestão de profissionais (cadastro, edição, desativação e reativação, com contato e formação
  opcionais) e de especialidades e valores de sessão.
- Consulta dos clientes cadastrados, com seus atendidos e planos.
- Painel gerencial: receita do mês, planos contratados, novos clientes, ranking de profissionais e
  desempenho por especialidade.

**Em todo o sistema**
- Páginas de erro amigáveis (403, 404, 405, 422 e 500) para quem usa o navegador, sem expor detalhes técnicos; os
  endpoints JSON, o webhook e os testes recebem o mesmo status e o mesmo JSON de sempre. O 404 é igual para "não
  existe" e "é de outro Cliente".
- Entradas inválidas (caractere nulo em campo ou URL, data e hora com fuso, ids malformados) são recusadas com 400 ou
  404 e uma mensagem clara, nunca com erro 500.

### Desconto por duração do plano (RN11)

| Duração | Desconto |
|---|---|
| Quinzenal | sem desconto |
| Mensal | 5% |
| Bimestral | 7% |
| Anual | 10% |

O desconto incide sobre o valor da quantidade real de sessões (valor da sessão × sessões geradas),
arredondado a duas casas, e fica registrado no plano. Os percentuais ficam em `DESCONTO_POR_DURACAO`,
em `app/core/config.py`.

## Status

| Semana | Entrega | Situação |
|---|---|---|
| 1 | Setup, banco e RF01 (cadastro, login, redefinição de senha) | Concluída |
| 2 | RF02 (perfil), RF03 (atendidos), RF11 (profissionais), RF12 (especialidades) | Concluída |
| 3 | RF04 (especialidades e profissionais) e RF05 (simulação e disponibilidade, feriados) | Concluída |
| 4 | RF05 (confirmação e renovação) e RF06 (pagamento via AbacatePay) | Concluída |
| 5 | Ajustes pedidos pela profissional da área (vocabulário, Primeiros passos, desconto por duração, perfil do profissional) e RF08 (agenda do profissional) | Concluída |
| 6 | RF07 (remarcação) | Concluída |
| 7 | RF09 (avaliações), RF10 (histórico) e acompanhamento dos atendidos pelo profissional | Concluída |
| 8 | Testes finais, segurança e privacidade (roteiro manual dos três perfis, refinamento visual das telas, páginas de erro, exploração adversarial com correções, suíte de demonstração) | Concluída |
| 9 | Documentação (DVP 2.0, README), melhorias do roteiro manual (contato do responsável e Meu perfil do profissional), botão de simulação de pagamento para testes e preparação da apresentação | Em andamento |

Também já fazem parte do projeto o RF14 (consulta de clientes pelo Admin), o RF15 (painel
gerencial) e o RF16 (Meus atendidos do Profissional). O RF13 (lembrete por WhatsApp) foi cancelado em 04/11 e não
será feito.

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
| `DEBUG` | `True` só em desenvolvimento: mostra detalhes técnicos dos erros e deixa o cookie de login funcionar em HTTP comum. O padrão é `False` (use assim na apresentação): o cookie de login passa a ser `Secure`, e o navegador só o envia por HTTPS ou por `localhost`/`127.0.0.1` (testado no Chrome). Abrir o sistema pelo IP da rede local em HTTP com `DEBUG=False` não mantém o login. |
| `MAIL_*` | Servidor SMTP dos e-mails (redefinição de senha e confirmação de pagamento). Em desenvolvimento, use o Email Sandbox do Mailtrap (gratuito), que intercepta os e-mails sem entregá-los a ninguém (também serve para a apresentação). Sem essas variáveis a aplicação funciona, mas nenhum e-mail é enviado (a falha de SMTP é apenas registrada no log). |
| `AVALIACOES_DIR` | Opcional. Pasta dos PDFs das avaliações; o padrão é `dados/avaliacoes`, dentro da pasta do projeto (um caminho relativo é sempre contado a partir da raiz do projeto). A pasta é criada sozinha, fica fora de `app/static` e não é versionada. |
| `ABACATEPAY_API_KEY` | Chave de API da AbacatePay em Dev mode (ver a seção de pagamento abaixo). |
| `ABACATEPAY_BASE_URL` | `https://api.abacatepay.com/v2` |
| `ABACATEPAY_WEBHOOK_SECRET` | Segredo do webhook, o mesmo valor cadastrado no painel da AbacatePay. |
| `SIMULACAO_PAGAMENTO_HABILITADA` | Opcional. `True` mostra o botão "Simular pagamento (ambiente de teste)" na cobrança Pix (só em Dev mode, para testes e demonstração). O padrão é `False`: com ele desligado o botão não aparece e a rota responde 404. Não deixe ligado fora de testes. |

Não deixe espaço depois do `=` e reinicie o servidor sempre que alterar o `.env`.

Os links enviados por e-mail (como o de redefinição de senha) usam o endereço pelo qual o sistema foi aberto no navegador (`localhost` ou o domínio do ngrok); não há variável de URL base no `.env`.

### 5. Criar o banco a partir das migrations

```bash
alembic upgrade head
```

Isso cria todas as tabelas e enums do banco a partir da migration inicial e das seguintes, em um
PostgreSQL vazio. **Não rode `alembic revision --autogenerate` ao configurar o projeto:** as
migrations já existem no repositório.

Ao alterar um model, gere uma nova migration e **abra o arquivo gerado antes de aplicar**,
conferindo que não há `op.drop_table(...)` inesperado (acontece quando um model novo não é
importado em `app/models/__init__.py`):

```bash
alembic revision --autogenerate -m "descrição da mudança"
alembic upgrade head
```

Para **renomear** uma tabela ou coluna, escreva a migration à mão (`op.rename_table`): o
`--autogenerate` trataria como remoção seguida de criação e apagaria os dados.

### 6. Criar o primeiro Administrador

Não existe autocadastro de Admin pela tela (o `/cadastro` cria só Clientes). Com o banco já
migrado, rode na raiz do projeto, com o ambiente virtual ativo:

```bash
python -m scripts.criar_admin
```

O script pede nome, e-mail e senha no terminal (a senha não aparece ao digitar e é pedida duas
vezes), valida com as mesmas regras do cadastro (e-mail válido e único, senha com pelo menos 8
caracteres) e grava o usuário com `tipo=admin` e a senha em hash. Depois é só entrar pelo
`/login`. Profissionais são cadastrados pelo próprio Admin, na tela **Profissionais**, e as
especialidades e os valores de sessão, na tela **Especialidades**.

### 7. Rodar a aplicação

```bash
uvicorn app.main:app --reload
```

Acesse http://localhost:8000. Deve aparecer a página inicial da NeuroClinic.

O log de acesso do `uvicorn` imprime a URL completa de cada requisição, inclusive o segredo do webhook e os links de
redefinição de senha. Não cole esse log em conversas e, em apresentações, suba com
`uvicorn app.main:app --no-access-log`.

### 8. Rodar os testes

```bash
pytest                                    # suíte completa: 2509 testes, cerca de 17 minutos
pytest tests/apresentacao -v --tb=short   # suíte de demonstração: 49 testes, poucos segundos
```

Os testes usam um banco temporário (SQLite em memória), então não tocam no PostgreSQL e não precisam de banco criado
nem do `.env` (o `conftest.py` fixa a configuração). O envio de e-mail e qualquer chamada de rede são bloqueados, e a
AbacatePay é substituída por uma versão falsa. As regras de negócio recebem a data e a hora por parâmetro, o que
permite testá-las com horários fixos, sem depender do relógio real.

A suíte de demonstração está dividida em `unitarios/` (regras puras, como o desconto da RN11 e os prazos da RN09),
`integracao/` (serviços com o banco de teste) e `sistema/` (fluxos completos pelas rotas, sem navegador). Como o SQLite
não é o PostgreSQL, os testes não exercitam travas de linha (`FOR UPDATE`) nem comportamentos próprios do PostgreSQL.

## Pagamento em desenvolvimento (AbacatePay, Dev mode)

O sistema cobra por Pix ou boleto pela API v2 da AbacatePay (`/v2/transparents/create`). O valor
cobrado é o total do plano já com o desconto da duração. O caminho principal de confirmação é o
**webhook** (evento `transparent.completed`): quando a AbacatePay avisa, as sessões do plano passam
de `pendente_pagamento` para `agendada`, o plano fica ativo e o e-mail de confirmação é enviado.
Como o servidor roda em `localhost`, o webhook precisa de um túnel público.

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

No Dev mode ninguém paga de verdade. Há um botão na tela e dois scripts, com papéis diferentes.

**Botão "Simular pagamento (ambiente de teste)"** (só Pix): aparece na página de cobrança quando `SIMULACAO_PAGAMENTO_HABILITADA=True` no `.env`. Ele só pede à AbacatePay que marque a cobrança como paga (a mesma chamada do script abaixo) e não grava nada no banco: a página de cobrança confere o pagamento na API logo em seguida e confirma pelo caminho normal, sem esperar o intervalo de 10 segundos. Por isso, ao usar o botão, quem confirma é a conferência (o log mostra "confirmado por conferência") e o webhook chega logo depois, encontra o pagamento já confirmado e só responde 200. Para ver o **webhook** confirmar, deixe a página de cobrança aberta e use o script `simular_pagamento` no terminal. Desligado (padrão), o botão não aparece e a rota responde 404; para boleto ele nunca aparece.

Os scripts:

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
- O botão "Simular pagamento" é uma ferramenta de teste: só funciona em Dev mode e deve ficar desligado (`SIMULACAO_PAGAMENTO_HABILITADA=False`) fora de testes e demonstrações.
- O ngrok e o servidor precisam estar rodando para o webhook funcionar. Antes de uma demonstração,
  suba os dois e faça um pagamento de teste; se o webhook falhar, a conferência cobre o Pix com a
  página de cobrança aberta.

## Regras de negócio

| Regra | Resumo |
|---|---|
| RN01 | Um profissional não pode ter duas sessões com períodos sobrepostos. |
| RN02 | A remarcação exige no mínimo 4 horas de antecedência. |
| RN03 | A avaliação só é acessível ao cliente responsável, ao Admin e ao profissional com sessão vinculada ao atendido. |
| RN04 | A avaliação só pode ser liberada depois de a sessão estar realizada. |
| RN05 | Profissional só pode ser desativado sem sessões futuras agendadas ou aguardando pagamento. |
| RN06 | Feriados nacionais não têm atendimento: a sessão é deslocada para a semana seguinte, sem mudar a quantidade de sessões nem o valor. |
| RN07 | A especialidade de um profissional só pode mudar nas mesmas condições da RN05. |
| RN08 | Sessões de 60 minutos, em hora cheia, de segunda a sexta, das 8h às 11h e das 14h às 17h. |
| RN09 | O plano reservado deve ser pago em até 4 horas (Pix) ou até o vencimento do boleto (menor entre 3 dias úteis e o 2º dia útil antes da primeira sessão). Depois do prazo, as sessões pendentes deixam de bloquear o horário e o plano é removido após a carência (60 minutos no Pix, 1 dia útil no boleto), nunca se houver sessão agendada, realizada ou pagamento confirmado. |
| RN10 | A primeira sessão de um plano precisa estar a pelo menos 4 horas de distância. |
| RN11 | O valor do plano recebe desconto conforme a duração (quinzenal 0%, mensal 5%, bimestral 7%, anual 10%), arredondado a duas casas, e o desconto aplicado fica registrado no plano. |

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
tests/        → testes automatizados (Pytest); tests/apresentacao/ é a suíte rápida de demonstração
```