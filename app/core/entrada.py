from fastapi import HTTPException, Request, status

MSG_CARACTERE_INVALIDO = "Os dados enviados contêm caracteres inválidos."
NUL = "\x00"
TIPOS_DE_FORMULARIO = ("application/x-www-form-urlencoded", "multipart/form-data")
ROTAS_SEM_FILTRO = {"/webhooks/abacatepay"}  # autenticação e corpo JSON próprios; o B5 trata o id inválido lá dentro


def _tem_nul(pares) -> bool:
    return any(NUL in chave or (isinstance(valor, str) and NUL in valor) for chave, valor in pares)


async def recusar_caractere_nul(request: Request) -> None:
    if getattr(request.scope.get("route"), "path", None) in ROTAS_SEM_FILTRO:
        return
    if _tem_nul(request.query_params.multi_items()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MSG_CARACTERE_INVALIDO)
    tipo = request.headers.get("content-type", "").lower()
    if tipo.startswith(TIPOS_DE_FORMULARIO):
        # O FastAPI já leu o formulário da rota neste mesmo Request: form() devolve o que está em cache, sem reler o upload
        formulario = await request.form()
        if _tem_nul(formulario.multi_items()):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MSG_CARACTERE_INVALIDO)
