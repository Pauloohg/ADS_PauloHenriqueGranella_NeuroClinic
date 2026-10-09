from datetime import time
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import DIAS_UTEIS, HORARIOS_ATENDIMENTO, PRAZO_PAGAMENTO_HORAS
from app.core.templating import moeda, templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, erros_por_campo, get_usuario_por_tipo
from app.schemas.cliente import PerfilClienteAtualizacao
from app.schemas.paciente import PacienteDados
from app.services import (
    cliente_service,
    contratacao_service,
    especialidade_service,
    funcionario_service,
    paciente_service,
    plano_service,
)

router = APIRouter(tags=["cliente"])

somente_cliente = get_usuario_por_tipo(TipoUsuario.CLIENTE)

TEMPLATE_PERFIL = "cliente/perfil.html"
TEMPLATE_PACIENTES = "cliente/pacientes.html"
TEMPLATE_PACIENTE_FORM = "cliente/paciente_form.html"
TEMPLATE_ESPECIALIDADES = "cliente/especialidades.html"
TEMPLATE_PROFISSIONAIS = "cliente/profissionais.html"
TEMPLATE_CONTRATAR = "cliente/contratar.html"
TEMPLATE_RESUMO = "cliente/contratar_resumo.html"

DURACAO_PADRAO = "mensal"
MSG_REVISE = "Revise os campos destacados antes de continuar."

MSGS_DADO_EM_USO = {
    "email": "Este e-mail já está em uso por outra conta.",
    "cpf": "Este CPF já está cadastrado em outra conta.",
}
MENSAGENS_PACIENTES = {
    "cadastrado": "Paciente cadastrado com sucesso.",
    "atualizado": "Dados do paciente atualizados.",
}


def _redirecionar(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


# --- RF02: perfil do Cliente ---------------------------------------------------------------------

@router.get("/perfil")
def perfil(request: Request, usuario: Usuario = Depends(somente_cliente), db: Session = Depends(get_db)):
    cliente = cliente_service.obter_cliente(db, usuario)
    dados = {"nome": usuario.nome, "email": usuario.email, "telefone": cliente.telefone or "", "cpf": cliente.cpf or ""}
    contexto = {"usuario": usuario, "dados": dados, "erros": {}, "sucesso": request.query_params.get("ok") == "1"}
    return templates.TemplateResponse(request, TEMPLATE_PERFIL, contexto)


@router.post("/perfil")
def atualizar_perfil(
    request: Request,
    nome: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    telefone: Annotated[str, Form()] = "",
    cpf: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    dados_form = {"nome": nome, "email": email, "telefone": telefone, "cpf": cpf}
    try:
        dados = PerfilClienteAtualizacao(**dados_form)
        cliente_service.atualizar_perfil(db, usuario, dados)
    except ValidationError as exc:
        erros = erros_por_campo(exc.errors())
        # Checar duplicidade mesmo com erro de validação, senão um CPF inválido esconde o e-mail repetido
        em_uso = cliente_service.campos_em_uso(db, usuario.id, email, cpf)
    except cliente_service.DadosEmUso as exc:
        erros, em_uso = {}, exc.campos
    else:
        return _redirecionar("/perfil?ok=1")
    for campo in em_uso:
        erros.setdefault(campo, MSGS_DADO_EM_USO[campo])
    return templates.TemplateResponse(
        request, TEMPLATE_PERFIL, {"usuario": usuario, "dados": dados_form, "erros": erros},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


# --- RF03: pacientes do Cliente ------------------------------------------------------------------

def _buscar_paciente_ou_404(db: Session, paciente_id: int, usuario: Usuario):
    try:
        return paciente_service.buscar_do_cliente(db, paciente_id, usuario.id)
    except paciente_service.PacienteNaoEncontrado:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado.") from None


def _form_paciente(request: Request, usuario: Usuario, dados: dict, erros: dict, paciente=None, status_code=200):
    contexto = {
        "usuario": usuario, "dados": dados, "erros": erros, "paciente": paciente,
        "hoje": agora_local().date().isoformat(),
    }
    return templates.TemplateResponse(request, TEMPLATE_PACIENTE_FORM, contexto, status_code=status_code)


def _salvar_paciente(request, usuario, db, nome, data_nascimento, paciente=None):
    dados_form = {"nome": nome, "data_nascimento": data_nascimento}
    hoje = agora_local().date()
    try:
        dados = PacienteDados(**dados_form)
        if paciente is None:
            paciente_service.cadastrar(db, usuario.id, dados, hoje)
        else:
            paciente_service.atualizar(db, paciente.id, usuario.id, dados, hoje)
    except ValidationError as exc:
        erros = erros_por_campo(exc.errors())
    except paciente_service.DataNascimentoInvalida as exc:
        erros = {"data_nascimento": str(exc)}
    else:
        return _redirecionar(f"/pacientes?ok={'cadastrado' if paciente is None else 'atualizado'}")
    return _form_paciente(request, usuario, dados_form, erros, paciente, status.HTTP_400_BAD_REQUEST)


@router.get("/pacientes")
def listar_pacientes(request: Request, usuario: Usuario = Depends(somente_cliente), db: Session = Depends(get_db)):
    contexto = {
        "usuario": usuario,
        "pacientes": paciente_service.listar_do_cliente(db, usuario.id),
        "sucesso": MENSAGENS_PACIENTES.get(request.query_params.get("ok")),
    }
    return templates.TemplateResponse(request, TEMPLATE_PACIENTES, contexto)


@router.get("/pacientes/novo")
def novo_paciente_form(request: Request, usuario: Usuario = Depends(somente_cliente)):
    return _form_paciente(request, usuario, {}, {})


@router.post("/pacientes/novo")
def cadastrar_paciente(
    request: Request,
    nome: Annotated[str, Form()] = "",
    data_nascimento: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    return _salvar_paciente(request, usuario, db, nome, data_nascimento)


@router.get("/pacientes/{paciente_id}/editar")
def editar_paciente_form(
    request: Request, paciente_id: int, usuario: Usuario = Depends(somente_cliente), db: Session = Depends(get_db)
):
    paciente = _buscar_paciente_ou_404(db, paciente_id, usuario)
    dados = {"nome": paciente.nome, "data_nascimento": paciente.data_nascimento.isoformat()}
    return _form_paciente(request, usuario, dados, {}, paciente)


@router.post("/pacientes/{paciente_id}/editar")
def editar_paciente(
    request: Request,
    paciente_id: int,
    nome: Annotated[str, Form()] = "",
    data_nascimento: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    paciente = _buscar_paciente_ou_404(db, paciente_id, usuario)
    return _salvar_paciente(request, usuario, db, nome, data_nascimento, paciente)


# --- RF04: especialidades e profissionais --------------------------------------------------------

@router.get("/especialidades")
def listar_especialidades(request: Request, usuario: Usuario = Depends(somente_cliente), db: Session = Depends(get_db)):
    contexto = {"usuario": usuario, "especialidades": especialidade_service.listar(db)}
    return templates.TemplateResponse(request, TEMPLATE_ESPECIALIDADES, contexto)


@router.get("/especialidades/{especialidade_id}/profissionais")
def listar_profissionais(
    request: Request, especialidade_id: int, usuario: Usuario = Depends(somente_cliente), db: Session = Depends(get_db)
):
    try:
        especialidade = especialidade_service.buscar(db, especialidade_id)
    except especialidade_service.EspecialidadeNaoEncontrada:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Especialidade não encontrada.") from None
    contexto = {
        "usuario": usuario,
        "especialidade": especialidade,
        "profissionais": funcionario_service.listar_ativos_da_especialidade(db, especialidade_id),
    }
    return templates.TemplateResponse(request, TEMPLATE_PROFISSIONAIS, contexto)


# --- RF05: contratação de plano (até a simulação) ------------------------------------------------

def _profissional_ativo_ou_404(db: Session, funcionario_id: int):
    try:
        plano_service.buscar_funcionario_ativo(db, funcionario_id)
    except plano_service.ProfissionalIndisponivel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profissional não encontrado.") from None
    return funcionario_service.buscar(db, funcionario_id)


def _duracao_ou_padrao(duracao: str | None) -> str:
    return duracao if duracao in plano_service.JANELA_POR_DURACAO else DURACAO_PADRAO


def _tela_contratacao(request, usuario, db, profissional, dados, erros=None, conflitos=(), erro=None, status_code=200):
    duracao = _duracao_ou_padrao(dados.get("duracao"))
    contexto = {
        "usuario": usuario,
        "profissional": profissional,
        "pacientes": paciente_service.listar_do_cliente(db, usuario.id),
        "pedir_cpf": cliente_service.obter_cliente(db, usuario).cpf is None,
        "livres": plano_service.disponibilidade_semanal(db, profissional.usuario.id, duracao, agora_local()),
        "dias_uteis": DIAS_UTEIS,
        "horarios": HORARIOS_ATENDIMENTO,
        "dados": {**dados, "duracao": duracao},
        "erros": erros or {},
        "conflitos": conflitos,
        "erro": erro,
    }
    return templates.TemplateResponse(request, TEMPLATE_CONTRATAR, contexto, status_code=status_code)


@router.get("/contratar/{funcionario_id}")
def contratar_form(
    request: Request,
    funcionario_id: int,
    paciente_id: str = "",
    frequencia_semanal: str = "1",
    duracao: str = DURACAO_PADRAO,
    celula: Annotated[list[str], Query()] = [],
    observacao: str = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    profissional = _profissional_ativo_ou_404(db, funcionario_id)
    dados = {
        "paciente_id": paciente_id, "frequencia_semanal": frequencia_semanal, "duracao": duracao,
        "celulas": celula, "observacao": observacao,
    }
    return _tela_contratacao(request, usuario, db, profissional, dados)


@router.get("/contratar/{funcionario_id}/disponibilidade")
def disponibilidade(
    funcionario_id: int,
    duracao: str = DURACAO_PADRAO,
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    _profissional_ativo_ou_404(db, funcionario_id)
    try:
        livres = plano_service.disponibilidade_semanal(db, funcionario_id, duracao, agora_local())
    except plano_service.PlanoInvalido as exc:
        return JSONResponse({"erro": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    return {"duracao": duracao, "livres": {str(dia): horas for dia, horas in livres.items()}}


def _ler_horario(horario: str) -> time:
    try:
        return time.fromisoformat(horario)
    except ValueError:
        raise plano_service.HorarioInvalido("Horário inválido.") from None


@router.get("/contratar/{funcionario_id}/simulacao")
def simulacao(
    funcionario_id: int,
    dias: Annotated[list[str], Query()] = [],
    horario: str = "",
    frequencia: str = "",
    duracao: str = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    _profissional_ativo_ou_404(db, funcionario_id)
    try:
        if not all(dia.isdigit() for dia in dias):
            raise plano_service.DiasSemanaInvalidos("Dia da semana inválido.")
        if not frequencia.isdigit():
            raise plano_service.FrequenciaInvalida(contratacao_service.MSG_FREQUENCIA)
        sim = plano_service.simular_plano(
            db, funcionario_id, [int(dia) for dia in dias], _ler_horario(horario), int(frequencia), duracao,
            agora_local(),
        )
    except plano_service.PlanoInvalido as exc:
        return JSONResponse({"erro": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    return {
        "quantidade": sim.quantidade,
        "valor_sessao": moeda(sim.valor_sessao),
        "valor_total": moeda(sim.valor_total),
        "data_inicio": sim.data_inicio.strftime("%d/%m/%Y"),
        "data_fim": sim.data_fim.strftime("%d/%m/%Y"),
        "feriados_pulados": [
            {"data": dia.strftime("%d/%m/%Y"), "nome": plano_service.nome_do_feriado(dia)}
            for dia in sim.feriados_pulados
        ],
        "conflitos": [data_hora.strftime("%d/%m/%Y às %H:%M") for data_hora in sim.conflitos],
    }


@router.post("/contratar/{funcionario_id}")
def contratar_resumo(
    request: Request,
    funcionario_id: int,
    paciente_id: Annotated[str, Form()] = "",
    frequencia_semanal: Annotated[str, Form()] = "",
    duracao: Annotated[str, Form()] = "",
    celula: Annotated[list[str], Form()] = [],
    observacao: Annotated[str, Form()] = "",
    cpf: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    profissional = _profissional_ativo_ou_404(db, funcionario_id)
    paciente = None
    if paciente_id.strip():
        if not paciente_id.strip().isdigit():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paciente não encontrado.")
        paciente = _buscar_paciente_ou_404(db, int(paciente_id), usuario)

    dados = {
        "paciente_id": paciente_id, "frequencia_semanal": frequencia_semanal, "duracao": duracao,
        "celulas": celula, "observacao": observacao, "cpf": cpf,
    }
    try:
        resumo = contratacao_service.preparar_resumo(
            db, cliente_service.obter_cliente(db, usuario), funcionario_id, paciente, celula,
            frequencia_semanal, duracao, observacao, cpf, agora_local(),
        )
    except contratacao_service.ContratacaoInvalida as exc:
        status_code = status.HTTP_409_CONFLICT if exc.conflitos else status.HTTP_400_BAD_REQUEST
        return _tela_contratacao(
            request, usuario, db, profissional, dados, exc.erros, exc.conflitos, MSG_REVISE, status_code
        )
    parametros = [(campo, dados[campo]) for campo in ("paciente_id", "frequencia_semanal", "duracao", "observacao")]
    contexto = {
        "usuario": usuario,
        "resumo": resumo,
        "feriados": [(dia, plano_service.nome_do_feriado(dia)) for dia in resumo.simulacao.feriados_pulados],
        "url_editar": f"/contratar/{funcionario_id}?" + urlencode(parametros + [("celula", c) for c in celula]),
        "prazo_horas": PRAZO_PAGAMENTO_HORAS,
    }
    return templates.TemplateResponse(request, TEMPLATE_RESUMO, contexto)
