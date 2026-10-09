(() => {
    const form = document.getElementById("form-contratacao");
    if (!form) return;

    const $ = (id) => document.getElementById(id);
    const celulas = () => [...form.querySelectorAll('input[name="celula"]')];
    const selecionadas = () => celulas().filter((c) => c.checked);
    const frequencia = () => Number(form.querySelector('input[name="frequencia_semanal"]:checked')?.value || 1);
    const duracao = $("duracao");
    const grade = $("grade");
    const botao = $("btn-continuar");
    let ultimaSimulacao = 0;

    function mostrar(id, visivel) {
        $(id).classList.toggle("d-none", !visivel);
    }

    function aplicarRegras() {
        const escolhidas = selecionadas();
        const hora = escolhidas.length ? escolhidas[0].dataset.hora : null;
        const completa = escolhidas.length >= frequencia();
        for (const celula of celulas()) {
            const livre = celula.dataset.livre === "1";
            celula.nextElementSibling.classList.toggle("celula-ocupada", !livre);
            if (celula.checked) {
                celula.disabled = false;
                continue;
            }
            celula.disabled = !livre || completa || (hora !== null && celula.dataset.hora !== hora);
        }
        const faltam = frequencia() - escolhidas.length;
        $("grade-instrucao").textContent = faltam > 0
            ? `Escolha ${faltam} horário(s) livre(s), todos na mesma hora e em dias diferentes. As sessões duram 60 minutos e se repetem toda semana.`
            : "Seleção completa. Para trocar, desmarque um horário.";
    }

    function limparResumo(mensagem) {
        $("resumo-instrucao").textContent = mensagem;
        mostrar("resumo-instrucao", true);
        for (const id of ["resumo-carregando", "resumo-erro", "resumo-valores", "resumo-feriados", "resumo-conflitos"]) {
            mostrar(id, false);
        }
    }

    function preencherLista(id, itens) {
        const lista = $(id);
        lista.replaceChildren(...itens.map((texto) => {
            const li = document.createElement("li");
            li.textContent = texto;
            return li;
        }));
    }

    async function simular() {
        const escolhidas = selecionadas();
        botao.disabled = true;
        if (escolhidas.length !== frequencia()) {
            limparResumo("Escolha os horários na grade para ver a quantidade de sessões e o valor.");
            return;
        }

        const parametros = new URLSearchParams({
            horario: `${escolhidas[0].dataset.hora.padStart(2, "0")}:00`,
            frequencia: String(frequencia()),
            duracao: duracao.value,
        });
        escolhidas.forEach((c) => parametros.append("dias", c.dataset.dia));

        const id = ++ultimaSimulacao;
        limparResumo("");
        mostrar("resumo-instrucao", false);
        mostrar("resumo-carregando", true);
        try {
            const resposta = await fetch(`${form.dataset.urlSimulacao}?${parametros}`, { headers: { Accept: "application/json" } });
            const dados = await resposta.json();
            if (id !== ultimaSimulacao) return;
            mostrar("resumo-carregando", false);
            if (!resposta.ok) {
                $("resumo-erro").textContent = dados.erro || "Não foi possível calcular o plano.";
                mostrar("resumo-erro", true);
                return;
            }
            $("resumo-quantidade").textContent = dados.quantidade;
            $("resumo-periodo").textContent = `${dados.data_inicio} a ${dados.data_fim}`;
            $("resumo-valor-sessao").textContent = dados.valor_sessao;
            $("resumo-valor-total").textContent = dados.valor_total;
            mostrar("resumo-valores", true);
            preencherLista("resumo-feriados-lista", dados.feriados_pulados.map((f) => `${f.data} — ${f.nome}`));
            mostrar("resumo-feriados", dados.feriados_pulados.length > 0);
            preencherLista("resumo-conflitos-lista", dados.conflitos);
            mostrar("resumo-conflitos", dados.conflitos.length > 0);
            botao.disabled = dados.conflitos.length > 0;
        } catch {
            if (id !== ultimaSimulacao) return;
            mostrar("resumo-carregando", false);
            $("resumo-erro").textContent = "Não foi possível calcular o valor agora. Verifique sua conexão e tente novamente.";
            mostrar("resumo-erro", true);
        }
    }

    async function recarregarGrade() {
        grade.setAttribute("aria-busy", "true");
        mostrar("grade-erro", false);
        botao.disabled = true;
        try {
            const resposta = await fetch(`${form.dataset.urlDisponibilidade}?duracao=${encodeURIComponent(duracao.value)}`);
            if (!resposta.ok) throw new Error();
            const { livres } = await resposta.json();
            for (const celula of celulas()) {
                const livre = (livres[celula.dataset.dia] || []).includes(Number(celula.dataset.hora));
                celula.dataset.livre = livre ? "1" : "0";
                if (!livre) celula.checked = false;
            }
        } catch {
            $("grade-erro").textContent = "Não foi possível atualizar os horários livres. Tente mudar a duração novamente.";
            mostrar("grade-erro", true);
        } finally {
            grade.setAttribute("aria-busy", "false");
            aplicarRegras();
            simular();
        }
    }

    form.addEventListener("change", (evento) => {
        const alvo = evento.target;
        if (alvo.name === "frequencia_semanal") {
            if (selecionadas().length > frequencia()) selecionadas().forEach((c) => { c.checked = false; });
        } else if (alvo.name === "duracao") {
            recarregarGrade();
            return;
        } else if (alvo.name !== "celula") {
            return;
        }
        $("alerta-conflitos")?.remove();
        aplicarRegras();
        simular();
    });

    aplicarRegras();
    simular();
})();
