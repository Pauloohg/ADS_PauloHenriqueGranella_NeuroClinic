import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as ArquivoRecebido

from app.core.config import TAMANHO_MAXIMO_AVALIACAO_MB
from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, agora_utc, get_usuario_por_tipo
from app.services import agenda_service, avaliacao_service
from app.services.admin_clientes_service import interpretar_pagina
from app.services.avaliacao_service import ArquivoInvalido, RegistroBloqueado, SessaoNaoEncontrada

logger = logging.getLogger(__name__)

somente_funcionario = get_usuario_por_tipo(TipoUsuario.FUNCIONARIO)

router = APIRouter(prefix="/agenda", tags=["agenda"], dependencies=[Depends(somente_funcionario)])

TEMPLATE_AGENDA = "funcionario/agenda.html"
TEMPLATE_REGISTRAR = "funcionario/registrar_atendimento.html"

MENSAGENS_REGISTRO = {
    "comparecimento": "Atendimento registrado. A avaliação foi enviada e o responsável vai receber um aviso por e-mail.",
    "falta": "Não comparecimento registrado. A sessão conta como realizada.",
}
MSG_FALHA_GRAVACAO = "Não foi possível salvar o arquivo agora. Nada foi registrado; tente novamente em instantes."


@router.get("")
def agenda(
    request: Request,
    visao: str | None = None,
    data: str | None = None,
    inicio: str | None = None,
    fim: str | None = None,
    pagina: str | None = None,
    registrado: str | None = None,
    usuario: Usuario = Depends(somente_funcionario),
    db: Session = Depends(get_db),
):
    agora = agora_local()
    hoje = agora.date()
    a_registrar = visao == agenda_service.A_REGISTRAR
    escolhida, erro = agenda_service.montar_visao(None if a_registrar else visao, data, inicio, fim, hoje)
    contexto = {
        "usuario": usuario,
        "visao": escolhida,
        "hoje": hoje,
        "a_registrar": a_registrar,
        "total_a_registrar": avaliacao_service.contar_a_registrar(db, usuario.id, agora),
        "sucesso": MENSAGENS_REGISTRO.get(registrado or ""),
        "erro": erro,
        "erro_no_periodo": erro is not None and visao == agenda_service.PERIODO,
        "form": {"data": data or "", "inicio": inicio or "", "fim": fim or ""},
    }
    if a_registrar:
        contexto["pendentes"] = avaliacao_service.consultas_a_registrar(db, usuario.id, agora, interpretar_pagina(pagina))
        return templates.TemplateResponse(request, TEMPLATE_AGENDA, contexto)

    consultas = agenda_service.consultas_do_profissional(db, usuario.id, escolhida.inicio, escolhida.fim, hoje, agora)
    contexto.update({
        "dias": agenda_service.agrupar_dias_vazios(agenda_service.agrupar_por_dia(escolhida, consultas), hoje),
        "total": len(consultas),
        "navegacao": agenda_service.navegacao(escolhida, hoje),
    })
    return templates.TemplateResponse(request, TEMPLATE_AGENDA, contexto)


# --- Registro do atendimento (RF09 / HU11 e HU12) ------------------------------------------------

def _sessao_ou_404(db: Session, usuario: Usuario, sessao_id: int, agora) -> avaliacao_service.SessaoParaRegistro:
    try:
        return avaliacao_service.buscar_sessao_do_profissional(db, usuario.id, sessao_id, agora.date())
    except SessaoNaoEncontrada:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta não encontrada.") from None


def _tela_registrar(request, usuario, consulta, agora, erro=None, erro_arquivo=None, status_code=200):
    contexto = {
        "usuario": usuario,
        "consulta": consulta,
        "bloqueio": avaliacao_service.motivo_bloqueio(consulta.status, consulta.data_hora, agora),
        "limite_mb": TAMANHO_MAXIMO_AVALIACAO_MB,
        "erro": erro,
        "erro_arquivo": erro_arquivo,
    }
    return templates.TemplateResponse(request, TEMPLATE_REGISTRAR, contexto, status_code=status_code)


def _redirecionar_para_o_dia(consulta, tipo: str) -> RedirectResponse:
    destino = f"/agenda?visao=dia&data={consulta.data_hora.date().isoformat()}&registrado={tipo}"
    return RedirectResponse(destino, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/sessoes/{sessao_id}/registrar")
def registrar_form(
    request: Request, sessao_id: int, usuario: Usuario = Depends(somente_funcionario), db: Session = Depends(get_db),
):
    agora = agora_local()
    return _tela_registrar(request, usuario, _sessao_ou_404(db, usuario, sessao_id, agora), agora)


@router.post("/sessoes/{sessao_id}/comparecimento")
def registrar_comparecimento(
    request: Request,
    sessao_id: int,
    tarefas: BackgroundTasks,
    # O navegador manda o campo como texto vazio quando nenhum arquivo foi escolhido; sem o str vira 400 genérico
    arquivo: Annotated[UploadFile | str | None, File()] = None,
    usuario: Usuario = Depends(somente_funcionario),
    db: Session = Depends(get_db),
):
    agora = agora_local()
    consulta = _sessao_ou_404(db, usuario, sessao_id, agora)
    enviado = arquivo if isinstance(arquivo, ArquivoRecebido) else None
    try:
        avaliacao_service.registrar_comparecimento(
            db, usuario.id, sessao_id,
            enviado.file if enviado else None, enviado.filename if enviado else None,
            agora, agora_utc().replace(tzinfo=None), str(request.base_url), tarefas,
        )
    except ArquivoInvalido as exc:
        return _tela_registrar(request, usuario, consulta, agora, erro_arquivo=exc.mensagem, status_code=400)
    except RegistroBloqueado as exc:
        consulta = _sessao_ou_404(db, usuario, sessao_id, agora)
        return _tela_registrar(request, usuario, consulta, agora, erro=exc.mensagem, status_code=409)
    except OSError:
        logger.exception("Falha ao gravar o arquivo da avaliação da sessão %s", sessao_id)
        return _tela_registrar(request, usuario, consulta, agora, erro=MSG_FALHA_GRAVACAO, status_code=503)
    return _redirecionar_para_o_dia(consulta, "comparecimento")


@router.post("/sessoes/{sessao_id}/nao-comparecimento")
def registrar_nao_comparecimento(
    request: Request, sessao_id: int, usuario: Usuario = Depends(somente_funcionario), db: Session = Depends(get_db),
):
    agora = agora_local()
    consulta = _sessao_ou_404(db, usuario, sessao_id, agora)
    try:
        avaliacao_service.registrar_nao_comparecimento(db, usuario.id, sessao_id, agora, agora_utc().replace(tzinfo=None))
    except RegistroBloqueado as exc:
        consulta = _sessao_ou_404(db, usuario, sessao_id, agora)
        return _tela_registrar(request, usuario, consulta, agora, erro=exc.mensagem, status_code=409)
    return _redirecionar_para_o_dia(consulta, "falta")
