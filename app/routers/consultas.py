from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import ANTECEDENCIA_REMARCACAO_HORAS, HORARIOS_ATENDIMENTO, JANELA_REMARCACAO_DIAS
from app.core.ids import inteiro_positivo
from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, agora_utc, get_usuario_por_tipo
from app.services import consultas_service, paciente_service, plano_service, remarcacao_service
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
    numero = inteiro_positivo(sessao_id)
    if numero is None:
        return None
    try:
        consulta = remarcacao_service.buscar_sessao_do_cliente(db, usuario.id, numero)
    except SessaoNaoEncontrada:
        return None
    historico = remarcacao_service.ultima_remarcacao(db, consulta.id)
    if historico is None:
        return None
    de, para = (valor.strftime("%d/%m/%Y às %H:%M") for valor in (historico.data_hora_original, historico.data_hora_nova))
    if repetido:
        return f"A consulta de {consulta.atendido} já estava remarcada para {para}. Nada foi alterado."
    return f"Consulta de {consulta.atendido} remarcada de {de} para {para}."


def _atendido_do_filtro(atendidos: list, texto: str | None):
    if not (texto or "").strip():
        return None
    numero = inteiro_positivo(texto)
    escolhido = next((a for a in atendidos if a.id == numero), None) if numero is not None else None
    if escolhido is None:  # inexistente, não numérico ou de outro Cliente
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atendido não encontrado.")
    return escolhido


@router.get("")
def listar(
    request: Request,
    aba: str = consultas_service.PROXIMAS,
    pagina: str | None = None,
    atendido: str | None = None,
    remarcada: str | None = None,
    repetido: str | None = None,
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    agora = agora_local()
    plano_service.remover_planos_vencidos(db, agora)
    atendidos = paciente_service.listar_do_cliente(db, usuario.id)
    filtro = _atendido_do_filtro(atendidos, atendido)
    filtro_id = filtro.id if filtro else None
    resultado = consultas_service.consultas_do_cliente(
        db, usuario.id, agora, aba, interpretar_pagina(pagina), paciente_id=filtro_id,
    )
    planos = consultas_service.planos_do_filtro(db, usuario.id, agora, filtro_id) if resultado.aba == consultas_service.PROXIMAS else []
    contexto = {
        "usuario": usuario,
        "resultado": resultado,
        "atendidos": atendidos,
        "filtro": filtro,
        "parametros_filtro": {"atendido": filtro_id} if filtro else {},
        "aguardando_pagamento": consultas_service.aguardando_pagamento(planos, agora),
        "filtro_sem_plano": filtro is not None and resultado.aba == consultas_service.PROXIMAS and not planos,
        "sucesso": _mensagem_da_lista(db, usuario, remarcada, repetido == "1"),
        "repetido": repetido == "1",
    }
    return templates.TemplateResponse(request, TEMPLATE_CONSULTAS, contexto)


def _novo_escolhido(escolhido: str, horarios) -> datetime | None:
    try:
        novo = remarcacao_service.interpretar_horario(escolhido)
    except RemarcacaoInvalida:
        return None
    return novo if horarios and novo in horarios.horarios else None


def _tela_remarcar(
    request, usuario, db, consulta, agora, data_texto="", escolhido="", erro=None, status_code=200, conflito=False,
):
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
        "feriado": (
            remarcacao_service.MSG_FERIADO.format(data=dia.strftime("%d/%m/%Y"), nome=plano_service.nome_do_feriado(dia))
            if horarios and horarios.motivo == remarcacao_service.DIA_FERIADO else ""
        ),
        "dia_lotado": remarcacao_service.MSG_DIA_LOTADO,
        "motivo_feriado": remarcacao_service.DIA_FERIADO,
        "motivo_lotado": remarcacao_service.DIA_SEM_HORARIOS,
        "escolhido": escolhido,
        "erro": erro,
        "conflito": conflito,
        "antecedencia_horas": ANTECEDENCIA_REMARCACAO_HORAS,
        "horas_atendimento": HORARIOS_ATENDIMENTO,
        "texto_troca": remarcacao_service.texto_troca(consulta.data_hora, _novo_escolhido(escolhido, horarios)),
        "nada_alterado": remarcacao_service.texto_nada_alterado(consulta.data_hora),
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
        conflito = isinstance(exc, remarcacao_service.ConflitoRemarcacao)
        codigo = status.HTTP_409_CONFLICT if conflito else status.HTTP_400_BAD_REQUEST
        return _tela_remarcar(
            request, usuario, db, consulta, agora, data_texto, novo_horario, None if conflito else exc.mensagem, codigo,
            conflito,
        )
    except SessaoNaoEncontrada:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta não encontrada.") from None
    sufixo = "&repetido=1" if resultado.repetido else ""
    return RedirectResponse(f"/consultas?remarcada={sessao_id}{sufixo}", status_code=status.HTTP_303_SEE_OTHER)


def _data_do_horario(texto: str) -> str:
    return texto.strip()[:10] if _ler_data(texto.strip()[:10]) else ""
