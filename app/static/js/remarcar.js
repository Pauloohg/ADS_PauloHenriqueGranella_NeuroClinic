(() => {
    const formData = document.getElementById("form-data");
    if (!formData) return;

    const campoData = document.getElementById("data");
    const area = document.getElementById("horarios");
    const confirmar = document.getElementById("btn-confirmar");
    let ultimaBusca = 0;

    function paragrafo(texto, classe) {
        const p = document.createElement("p");
        p.className = classe;
        p.textContent = texto;
        return p;
    }

    function mostrarMensagem(texto, classe = "remarcar-motivo mb-0") {
        area.replaceChildren(paragrafo(texto, classe));
        confirmar.classList.add("d-none");
    }

    function mostrarHorarios(dados) {
        const fieldset = document.createElement("fieldset");
        const legenda = document.createElement("legend");
        legenda.className = "form-label fs-6";
        const [ano, mes, dia] = dados.data.split("-");
        legenda.textContent = `Horários livres em ${dia}/${mes}/${ano}`;
        const grupo = document.createElement("div");
        grupo.className = "remarcar-horarios";
        dados.horarios.forEach((horario, indice) => {
            const radio = document.createElement("input");
            Object.assign(radio, { type: "radio", className: "btn-check", name: "novo_horario", id: `h-${indice + 1}`, value: horario.valor, required: true });
            const rotulo = document.createElement("label");
            rotulo.className = "btn btn-outline-primary font-monospace-num";
            rotulo.htmlFor = radio.id;
            rotulo.textContent = horario.rotulo;
            grupo.append(radio, rotulo);
        });
        fieldset.append(legenda, grupo);
        area.replaceChildren(fieldset);
        confirmar.classList.remove("d-none");
    }

    async function buscar() {
        if (!campoData.value) {
            mostrarMensagem("Escolha uma data para ver os horários livres.", "text-muted small mb-0");
            return;
        }
        const busca = ++ultimaBusca;
        mostrarMensagem("Buscando horários livres...", "text-muted small mb-0");
        try {
            const url = `${formData.dataset.urlHorarios}?data=${encodeURIComponent(campoData.value)}`;
            const resposta = await fetch(url, { headers: { Accept: "application/json" } });
            const dados = await resposta.json();
            if (busca !== ultimaBusca) return;
            if (!resposta.ok) {
                mostrarMensagem(dados.erro || "Não foi possível buscar os horários. Tente novamente.");
            } else if (dados.motivo) {
                mostrarMensagem(`${dados.motivo}. Escolha outra data.`);
            } else {
                mostrarHorarios(dados);
            }
        } catch {
            if (busca === ultimaBusca) mostrarMensagem("Não foi possível buscar os horários. Tente novamente.");
        }
    }

    formData.addEventListener("submit", (evento) => {
        evento.preventDefault();
        buscar();
    });
    campoData.addEventListener("change", buscar);
})();
