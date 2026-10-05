from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, erros_por_campo, get_usuario_por_tipo
from app.schemas.cliente import PerfilClienteAtualizacao
from app.schemas.paciente import PacienteDados
from app.services import cliente_service, paciente_service

router = APIRouter(tags=["cliente"])

somente_cliente = get_usuario_por_tipo(TipoUsuario.CLIENTE)

TEMPLATE_PERFIL = "cliente/perfil.html"
TEMPLATE_PACIENTES = "cliente/pacientes.html"
TEMPLATE_PACIENTE_FORM = "cliente/paciente_form.html"

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
