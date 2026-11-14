(() => {
    const formData = document.getElementById("form-data");
    if (!formData) return;

    const campoData = document.getElementById("data");
    const area = document.getElementById("horarios");
    const confirmar = document.getElementById("btn-confirmar");
    const horas = formData.dataset.horas.split(",").map(Number);
    const DIAS = ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"];
    const MOTIVO_FERIADO = "Feriado nacional";
    const MOTIVO_LOTADO = "Sem horários livres";
    let ultimaBusca = 0;

    function paragrafo(texto, classe) {
        const p = document.createElement("p");
        p.className = classe;
        p.textContent = texto;
        return p;
    }

    function trecho(texto, classe) {
        const span = document.createElement("span");
        span.className = classe;
        span.textContent = texto;
        return span;
    }

    function bloco(texto, classe) {
        const div = document.createElement("div");
        div.className = `${classe} remarcar-aviso`;
        div.id = "horarios-motivo";
        div.setAttribute("role", "status");
        div.textContent = texto;
        return div;
    }

    function mostrarMensagem(texto, classe = "text-muted small mb-0") {
        area.replaceChildren(paragrafo(texto, classe));
        confirmar.classList.add("d-none");
    }

    function celula(data, hora, livres) {
        const valor = `${data}T${String(hora).padStart(2, "0")}:00`;
        const rotuloHora = `${String(hora).padStart(2, "0")}:00`;
        const livre = livres.has(valor);
        const radio = document.createElement("input");
        Object.assign(radio, { type: "radio", className: "btn-check", name: "novo_horario", id: `h-${hora}`, value: valor, required: true, disabled: !livre });
        const rotulo = document.createElement("label");
        rotulo.className = livre ? "celula" : "celula celula-ocupada";
        rotulo.htmlFor = radio.id;
        rotulo.append(trecho(rotuloHora, "celula-livre font-monospace-num"), trecho("ocupado", "celula-indisponivel"));
        if (!livre) rotulo.append(trecho(`${rotuloHora}, `, "visually-hidden"));
        return [radio, rotulo];
    }

    // Ocupado = os horários da RN08 (data-horas) que não vieram na lista de livres do endpoint
    function grade(dados, rotuloTitulo = "Horários livres") {
        const [ano, mes, dia] = dados.data.split("-");
        const nomeDia = DIAS[new Date(Number(ano), Number(mes) - 1, Number(dia)).getDay()];
        const livres = new Set(dados.horarios.map((h) => h.valor));
        const titulo = document.createElement("h3");
        titulo.className = "remarcar-dia-titulo";
        titulo.id = "titulo-horarios";
        titulo.textContent = `${rotuloTitulo} na ${nomeDia}, ${dia}/${mes}`;
        const partes = [titulo];
        for (const [rotulo, turno] of [["Manhã", horas.filter((h) => h < 12)], ["Tarde", horas.filter((h) => h >= 12)]]) {
            const fieldset = document.createElement("fieldset");
            fieldset.className = "remarcar-turno";
            const legenda = document.createElement("legend");
            legenda.className = "remarcar-turno-rotulo";
            legenda.textContent = rotulo;
            const celulas = document.createElement("div");
            celulas.className = "remarcar-celulas";
            turno.forEach((hora) => celulas.append(...celula(dados.data, hora, livres)));
            fieldset.append(legenda, celulas);
            partes.push(fieldset);
        }
        return partes;
    }

    function mostrarHorarios(dados) {
        area.replaceChildren(...grade(dados));
        confirmar.classList.remove("d-none");
    }

    function mostrarMotivo(dados) {
        if (dados.motivo === MOTIVO_LOTADO) {
            area.replaceChildren(bloco("Não há horários livres neste dia. Escolha outra data.", "rev-feriado"), ...grade(dados, "Horários"));
        } else {
            const classe = dados.motivo === MOTIVO_FERIADO ? "rev-prazo" : "rev-feriado";
            area.replaceChildren(bloco(`${dados.motivo}. Escolha outra data.`, classe));
        }
        confirmar.classList.add("d-none");
    }

    async function buscar() {
        if (!campoData.value) {
            mostrarMensagem("Escolha uma data para ver os horários livres.");
            return;
        }
        const busca = ++ultimaBusca;
        mostrarMensagem("Buscando horários livres...");
        try {
            const url = `${formData.dataset.urlHorarios}?data=${encodeURIComponent(campoData.value)}`;
            const resposta = await fetch(url, { headers: { Accept: "application/json" } });
            const dados = await resposta.json();
            if (busca !== ultimaBusca) return;
            if (!resposta.ok) {
                mostrarMensagem(dados.erro || "Não foi possível buscar os horários. Tente novamente.", "remarcar-motivo mb-0");
            } else if (dados.motivo) {
                mostrarMotivo(dados);
            } else {
                mostrarHorarios(dados);
            }
        } catch {
            if (busca === ultimaBusca) mostrarMensagem("Não foi possível buscar os horários. Tente novamente.", "remarcar-motivo mb-0");
        }
    }

    formData.addEventListener("submit", (evento) => {
        evento.preventDefault();
        buscar();
    });
    campoData.addEventListener("change", buscar);
})();
