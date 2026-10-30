from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import JANELA_REMARCACAO_DIAS
from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, agora_utc, get_usuario_por_tipo
from app.services import consultas_service, plano_service, remarcacao_service
from app.services.admin_clientes_service import interpretar_pagina
from app.services.remarcacao_service import RemarcacaoInvalida, SessaoNaoEncontrada

somente_cliente = get_usuario_por_tipo(TipoUsuario.CLIENTE)

router = APIRouter(prefix="/consultas", tags=["consultas"], dependencies=[Depends(somente_cliente)])

TEMPLATE_CONSULTAS = "cliente/consultas.html"
TEMPLATE_REMARCAR = "cliente/remarcar.html"
MSG_DATA_INVALIDA = "Informe uma data válida, no formato AAAA-MM-DD."


def _sessao_ou_404(db: Session, usuario: Usuario, sessao_id: int) -> remarcacao_service.SessaoDoCliente:
    try:
        return remarcacao_service.buscar_sessao_do_cliente(db, usuario.id, sessao_id)
    except SessaoNaoEncontrada:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta não encontrada.") from None


def _ler_data(texto: str | None) -> date | None:
    try:
        return date.fromisoformat((texto or "").strip())
    except ValueError:
        return None


def _mensagem_da_lista(db: Session, usuario: Usuario, sessao_id: str | None, repetido: bool) -> str | None:
    if not sessao_id or not sessao_id.isdigit():
        return None
    try:
        consulta = remarcacao_service.buscar_sessao_do_cliente(db, usuario.id, int(sessao_id))
    except SessaoNaoEncontrada:
        return None
    historico = remarcacao_service.ultima_remarcacao(db, consulta.id)
    if historico is None:
        return None
    de, para = (valor.strftime("%d/%m/%Y às %H:%M") for valor in (historico.data_hora_original, historico.data_hora_nova))
    if repetido:
        return f"A consulta de {consulta.atendido} já estava remarcada para {para}. Nada foi alterado."
    return f"Consulta de {consulta.atendido} remarcada de {de} para {para}."


@router.get("")
def listar(
    request: Request,
    aba: str = consultas_service.PROXIMAS,
    pagina: str | None = None,
    remarcada: str | None = None,
    repetido: str | None = None,
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    agora = agora_local()
    plano_service.remover_planos_vencidos(db, agora)
    resultado = consultas_service.consultas_do_cliente(db, usuario.id, agora, aba, interpretar_pagina(pagina))
    contexto = {
        "usuario": usuario,
        "resultado": resultado,
        "aguardando_pagamento": (
            consultas_service.planos_aguardando_pagamento(db, usuario.id, agora)
            if resultado.aba == consultas_service.PROXIMAS else []
        ),
        "sucesso": _mensagem_da_lista(db, usuario, remarcada, repetido == "1"),
        "repetido": repetido == "1",
    }
    return templates.TemplateResponse(request, TEMPLATE_CONSULTAS, contexto)


def _tela_remarcar(request, usuario, db, consulta, agora, data_texto="", escolhido="", erro=None, status_code=200):
    inicio, fim = remarcacao_service.janela(agora)
    dia = _ler_data(data_texto) if data_texto else None
    bloqueio = None
    try:
        remarcacao_service.validar_sessao_remarcavel(consulta.status, consulta.data_hora, agora)
    except RemarcacaoInvalida as exc:
        bloqueio = exc.mensagem
    horarios = remarcacao_service.horarios_livres(db, consulta, dia, agora) if dia and not bloqueio else None
    contexto = {
        "usuario": usuario,
        "consulta": consulta,
        "fim_atual": consulta.data_hora + plano_service.DURACAO_SESSAO,
        "bloqueio": bloqueio,
        "janela_inicio": inicio,
        "janela_fim": fim,
        "janela_dias": JANELA_REMARCACAO_DIAS,
        "data_texto": data_texto,
        "data_invalida": bool(data_texto) and dia is None,
        "horarios": horarios,
        "feriado": plano_service.nome_do_feriado(dia) if horarios and horarios.motivo == remarcacao_service.DIA_FERIADO else "",
        "escolhido": escolhido,
        "erro": erro,
    }
    return templates.TemplateResponse(request, TEMPLATE_REMARCAR, contexto, status_code=status_code)


@router.get("/{sessao_id}/remarcar")
def remarcar_form(
    request: Request,
    sessao_id: int,
    data: str = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    consulta = _sessao_ou_404(db, usuario, sessao_id)
    return _tela_remarcar(request, usuario, db, consulta, agora_local(), data_texto=data)


@router.get("/{sessao_id}/horarios")
def horarios(
    sessao_id: int,
    data: str = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    consulta = _sessao_ou_404(db, usuario, sessao_id)
    dia = _ler_data(data)
    if dia is None:
        return JSONResponse({"erro": MSG_DATA_INVALIDA}, status_code=status.HTTP_400_BAD_REQUEST)
    agora = agora_local()
    try:
        remarcacao_service.validar_sessao_remarcavel(consulta.status, consulta.data_hora, agora)
    except RemarcacaoInvalida as exc:
        return JSONResponse({"erro": exc.mensagem}, status_code=status.HTTP_409_CONFLICT)
    livres = remarcacao_service.horarios_livres(db, consulta, dia, agora)
    return {
        "data": livres.data.isoformat(),
        "horarios": [{"valor": h.isoformat(timespec="minutes"), "rotulo": h.strftime("%H:%M")} for h in livres.horarios],
        "motivo": livres.motivo,
    }


@router.post("/{sessao_id}/remarcar")
def remarcar(
    request: Request,
    sessao_id: int,
    novo_horario: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    _sessao_ou_404(db, usuario, sessao_id)
    agora = agora_local()
    try:
        novo = remarcacao_service.interpretar_horario(novo_horario)
        resultado = remarcacao_service.remarcar(
            db, usuario.id, sessao_id, novo, agora, agora_utc().replace(tzinfo=None)
        )
    except RemarcacaoInvalida as exc:
        consulta = _sessao_ou_404(db, usuario, sessao_id)
        data_texto = _data_do_horario(novo_horario)
        codigo = (
            status.HTTP_409_CONFLICT if isinstance(exc, remarcacao_service.ConflitoRemarcacao)
            else status.HTTP_400_BAD_REQUEST
        )
        return _tela_remarcar(request, usuario, db, consulta, agora, data_texto, novo_horario, exc.mensagem, codigo)
    except SessaoNaoEncontrada:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta não encontrada.") from None
    sufixo = "&repetido=1" if resultado.repetido else ""
    return RedirectResponse(f"/consultas?remarcada={sessao_id}{sufixo}", status_code=status.HTTP_303_SEE_OTHER)


def _data_do_horario(texto: str) -> str:
    return texto.strip()[:10] if _ler_data(texto.strip()[:10]) else ""
