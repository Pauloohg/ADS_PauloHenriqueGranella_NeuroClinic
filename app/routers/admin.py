from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.templating import templates, valor_formulario
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, agora_utc, erros_por_campo, get_usuario_por_tipo
from app.schemas.especialidade import EspecialidadeCadastro, EspecialidadeValor
from app.schemas.funcionario import FuncionarioCadastro, FuncionarioEdicao
from app.services import auth_service, especialidade_service, funcionario_service

somente_admin = get_usuario_por_tipo(TipoUsuario.ADMIN)

# Proteção no router inteiro: rota nova em /admin já nasce restrita, mesmo se esquecer o Depends nela
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(somente_admin)])

TEMPLATE_FUNCIONARIOS = "admin/funcionarios.html"
TEMPLATE_FUNCIONARIO_FORM = "admin/funcionario_form.html"
TEMPLATE_ESPECIALIDADES = "admin/especialidades.html"
TEMPLATE_ESPECIALIDADE_FORM = "admin/especialidade_form.html"

MSG_ESPECIALIDADE_INVALIDA = "Selecione uma especialidade da lista."
MSG_FUNCIONARIO_COM_SESSOES = "Não é possível desativar {nome}: ele possui sessões futuras agendadas."
MSG_ESPECIALIDADE_COM_SESSOES = (
    "Não é possível alterar a especialidade de {nome}: ele possui sessões futuras agendadas ou aguardando pagamento."
)

MENSAGENS_FUNCIONARIOS = {
    "cadastrado": "Funcionário cadastrado. Informe a ele a senha provisória para o primeiro acesso.",
    "atualizado": "Dados do funcionário atualizados.",
    "desativado": "Funcionário desativado.",
    "reativado": "Funcionário reativado.",
}
MENSAGENS_ESPECIALIDADES = {
    "cadastrada": "Especialidade cadastrada.",
    "atualizada": "Valor da sessão atualizado. Planos já contratados mantêm o valor anterior.",
}


def _redirecionar(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _nao_encontrado(mensagem: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=mensagem)


# --- RF11: funcionários --------------------------------------------------------------------------

def _lista_funcionarios(request: Request, usuario: Usuario, db: Session, erro: str | None = None, status_code=200):
    contexto = {
        "usuario": usuario,
        "funcionarios": funcionario_service.listar(db),
        "sucesso": MENSAGENS_FUNCIONARIOS.get(request.query_params.get("ok")),
        "erro": erro,
    }
    return templates.TemplateResponse(request, TEMPLATE_FUNCIONARIOS, contexto, status_code=status_code)


def _form_funcionario(request, usuario, db, dados, erros, funcionario=None, status_code=200, erro=None):
    contexto = {
        "usuario": usuario, "dados": dados, "erros": erros, "funcionario": funcionario, "erro": erro,
        "especialidades": especialidade_service.listar(db),
    }
    return templates.TemplateResponse(request, TEMPLATE_FUNCIONARIO_FORM, contexto, status_code=status_code)


def _buscar_funcionario_ou_404(db: Session, funcionario_id: int):
    try:
        return funcionario_service.buscar(db, funcionario_id)
    except funcionario_service.FuncionarioNaoEncontrado:
        raise _nao_encontrado("Funcionário não encontrado.") from None


@router.get("/funcionarios")
def listar_funcionarios(request: Request, usuario: Usuario = Depends(somente_admin), db: Session = Depends(get_db)):
    return _lista_funcionarios(request, usuario, db)


@router.get("/funcionarios/novo")
def novo_funcionario_form(request: Request, usuario: Usuario = Depends(somente_admin), db: Session = Depends(get_db)):
    return _form_funcionario(request, usuario, db, {}, {})


@router.post("/funcionarios/novo")
def cadastrar_funcionario(
    request: Request,
    nome: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    especialidade_id: Annotated[str, Form()] = "",
    senha: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_admin),
    db: Session = Depends(get_db),
):
    dados_form = {"nome": nome, "email": email, "especialidade_id": especialidade_id}
    try:
        dados = FuncionarioCadastro(**dados_form, senha=senha)
        funcionario_service.cadastrar(db, dados, agora_utc())
    except ValidationError as exc:
        erros = erros_por_campo(exc.errors())
    except especialidade_service.EspecialidadeNaoEncontrada:
        erros = {"especialidade_id": MSG_ESPECIALIDADE_INVALIDA}
    except auth_service.EmailJaCadastrado:
        erros = {"email": "Já existe uma conta com este e-mail."}
    else:
        return _redirecionar("/admin/funcionarios?ok=cadastrado")
    return _form_funcionario(request, usuario, db, dados_form, erros, status_code=status.HTTP_400_BAD_REQUEST)


@router.get("/funcionarios/{funcionario_id}/editar")
def editar_funcionario_form(
    request: Request, funcionario_id: int, usuario: Usuario = Depends(somente_admin), db: Session = Depends(get_db)
):
    resumo = _buscar_funcionario_ou_404(db, funcionario_id)
    dados = {"nome": resumo.usuario.nome, "especialidade_id": str(resumo.funcionario.especialidade_id)}
    return _form_funcionario(request, usuario, db, dados, {}, resumo)


@router.post("/funcionarios/{funcionario_id}/editar")
def editar_funcionario(
    request: Request,
    funcionario_id: int,
    nome: Annotated[str, Form()] = "",
    especialidade_id: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_admin),
    db: Session = Depends(get_db),
):
    resumo = _buscar_funcionario_ou_404(db, funcionario_id)
    dados_form = {"nome": nome, "especialidade_id": especialidade_id}
    try:
        funcionario_service.atualizar(db, funcionario_id, FuncionarioEdicao(**dados_form), agora_local())
    except ValidationError as exc:
        erros = erros_por_campo(exc.errors())
    except especialidade_service.EspecialidadeNaoEncontrada:
        erros = {"especialidade_id": MSG_ESPECIALIDADE_INVALIDA}
    except funcionario_service.EspecialidadeComSessoesFuturas:
        erro = MSG_ESPECIALIDADE_COM_SESSOES.format(nome=resumo.usuario.nome)
        return _form_funcionario(request, usuario, db, dados_form, {}, resumo, status.HTTP_409_CONFLICT, erro)
    else:
        return _redirecionar("/admin/funcionarios?ok=atualizado")
    return _form_funcionario(request, usuario, db, dados_form, erros, resumo, status.HTTP_400_BAD_REQUEST)


@router.post("/funcionarios/{funcionario_id}/desativar")
def desativar_funcionario(
    request: Request, funcionario_id: int, usuario: Usuario = Depends(somente_admin), db: Session = Depends(get_db)
):
    resumo = _buscar_funcionario_ou_404(db, funcionario_id)
    try:
        funcionario_service.desativar(db, funcionario_id, agora_local())
    except funcionario_service.FuncionarioComSessoesAgendadas:
        erro = MSG_FUNCIONARIO_COM_SESSOES.format(nome=resumo.usuario.nome)
        return _lista_funcionarios(request, usuario, db, erro, status.HTTP_409_CONFLICT)
    return _redirecionar("/admin/funcionarios?ok=desativado")


@router.post("/funcionarios/{funcionario_id}/reativar")
def reativar_funcionario(funcionario_id: int, db: Session = Depends(get_db)):
    _buscar_funcionario_ou_404(db, funcionario_id)
    funcionario_service.reativar(db, funcionario_id)
    return _redirecionar("/admin/funcionarios?ok=reativado")


# --- RF12: especialidades ------------------------------------------------------------------------

def _form_especialidade(request, usuario, dados, erros, especialidade=None, status_code=200):
    contexto = {"usuario": usuario, "dados": dados, "erros": erros, "especialidade": especialidade}
    return templates.TemplateResponse(request, TEMPLATE_ESPECIALIDADE_FORM, contexto, status_code=status_code)


def _buscar_especialidade_ou_404(db: Session, especialidade_id: int):
    try:
        return especialidade_service.buscar(db, especialidade_id)
    except especialidade_service.EspecialidadeNaoEncontrada:
        raise _nao_encontrado("Especialidade não encontrada.") from None


@router.get("/especialidades")
def listar_especialidades(request: Request, usuario: Usuario = Depends(somente_admin), db: Session = Depends(get_db)):
    contexto = {
        "usuario": usuario,
        "especialidades": especialidade_service.listar(db),
        "sucesso": MENSAGENS_ESPECIALIDADES.get(request.query_params.get("ok")),
    }
    return templates.TemplateResponse(request, TEMPLATE_ESPECIALIDADES, contexto)


@router.get("/especialidades/nova")
def nova_especialidade_form(request: Request, usuario: Usuario = Depends(somente_admin)):
    return _form_especialidade(request, usuario, {}, {})


@router.post("/especialidades/nova")
def cadastrar_especialidade(
    request: Request,
    nome: Annotated[str, Form()] = "",
    valor_sessao: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_admin),
    db: Session = Depends(get_db),
):
    dados_form = {"nome": nome, "valor_sessao": valor_sessao}
    try:
        especialidade_service.cadastrar(db, EspecialidadeCadastro(**dados_form))
    except ValidationError as exc:
        erros = erros_por_campo(exc.errors())
    except especialidade_service.EspecialidadeJaCadastrada:
        erros = {"nome": "Já existe uma especialidade com este nome."}
    else:
        return _redirecionar("/admin/especialidades?ok=cadastrada")
    return _form_especialidade(request, usuario, dados_form, erros, status_code=status.HTTP_400_BAD_REQUEST)


@router.get("/especialidades/{especialidade_id}/editar")
def editar_especialidade_form(
    request: Request, especialidade_id: int, usuario: Usuario = Depends(somente_admin), db: Session = Depends(get_db)
):
    especialidade = _buscar_especialidade_ou_404(db, especialidade_id)
    dados = {"valor_sessao": valor_formulario(especialidade.valor_sessao)}
    return _form_especialidade(request, usuario, dados, {}, especialidade)


@router.post("/especialidades/{especialidade_id}/editar")
def editar_especialidade(
    request: Request,
    especialidade_id: int,
    valor_sessao: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_admin),
    db: Session = Depends(get_db),
):
    especialidade = _buscar_especialidade_ou_404(db, especialidade_id)
    try:
        especialidade_service.atualizar_valor(db, especialidade_id, EspecialidadeValor(valor_sessao=valor_sessao))
    except ValidationError as exc:
        erros = erros_por_campo(exc.errors())
    else:
        return _redirecionar("/admin/especialidades?ok=atualizada")
    return _form_especialidade(
        request, usuario, {"valor_sessao": valor_sessao}, erros, especialidade, status.HTTP_400_BAD_REQUEST
    )
