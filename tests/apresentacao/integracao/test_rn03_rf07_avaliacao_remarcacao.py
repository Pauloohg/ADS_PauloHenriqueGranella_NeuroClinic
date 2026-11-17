from datetime import date, datetime

from sqlalchemy import select

from app.models.historico_remarcacao import HistoricoRemarcacao
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services import remarcacao_service
from app.services.avaliacao_service import pode_acessar_avaliacao
from tests.fabricas import criar_plano

AGORA = datetime(2026, 8, 3, 9, 0)
AGORA_UTC = datetime(2026, 8, 3, 12, 0)
SEGUNDAS_14H = [datetime(2026, 8, dia, 14, 0) for dia in (17, 24, 31)]


def test_rn03_profissional_com_consulta_do_atendido_acessa_a_avaliacao(db, carla, maria, pedro):
    criar_plano(db, pedro, carla, [(SEGUNDAS_14H[0], StatusSessao.AGENDADA)])

    assert pode_acessar_avaliacao(db, carla.usuario, pedro.id, maria.id) is True


def test_rn03_profissional_sem_vinculo_ou_so_com_reserva_nao_acessa(db, carla, bruno, maria, pedro):
    criar_plano(db, pedro, carla, [(SEGUNDAS_14H[0], StatusSessao.PENDENTE_PAGAMENTO)])

    assert pode_acessar_avaliacao(db, carla.usuario, pedro.id, maria.id) is False  # reserva não é vínculo
    assert pode_acessar_avaliacao(db, bruno.usuario, pedro.id, maria.id) is False  # nenhum plano


def test_rn03_cliente_dono_e_admin_acessam_outro_cliente_nao(db, maria, joana, admin_ana, pedro):
    assert pode_acessar_avaliacao(db, maria, pedro.id, maria.id) is True
    assert pode_acessar_avaliacao(db, admin_ana, pedro.id, maria.id) is True
    assert pode_acessar_avaliacao(db, joana, pedro.id, maria.id) is False


def test_rf07_remarcar_atualiza_data_fim_e_grava_historico(db, carla, maria, pedro):
    plano = criar_plano(db, pedro, carla, [(d, StatusSessao.AGENDADA) for d in SEGUNDAS_14H])
    ultima = db.scalar(select(Sessao).where(Sessao.data_hora == SEGUNDAS_14H[-1]))
    novo = datetime(2026, 9, 4, 10, 0)  # sexta seguinte

    remarcacao_service.remarcar(db, maria.id, ultima.id, novo, AGORA, AGORA_UTC)

    db.expire_all()
    historico = db.scalars(select(HistoricoRemarcacao)).one()
    assert (historico.data_hora_original, historico.data_hora_nova) == (SEGUNDAS_14H[-1], novo)
    assert (db.get(Sessao, ultima.id).data_hora, db.get(Sessao, ultima.id).foi_remarcada) == (novo, True)
    assert (db.get(Plano, plano.id).data_inicio, db.get(Plano, plano.id).data_fim) == (date(2026, 8, 17), date(2026, 9, 4))
