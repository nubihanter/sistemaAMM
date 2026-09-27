import logging
from datetime import datetime
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from django_apscheduler.jobstores import DjangoJobStore
from django_apscheduler.models import DjangoJobExecution
from django_apscheduler import util

logger = logging.getLogger(__name__)


from sales.services import (
    registrar_execucao_sincronizacao,
    sincronizar_desde_ultimo_registro,
    sincronizar_metas_piperun,
)
from inventory.services import sincronizar_estoque_e_itens_rapido


# 1. Fecha conexões antigas do banco antes de cada execução
@util.close_old_connections
def tarefa_sync_hardness():
    """Roda a sincronização rápida (último registro até hoje) de notas, estoque e itens."""
    print("\n⏰ [Scheduler] Executando sync_hardness e sync_inventory rápida...")
    try:
        registrar_execucao_sincronizacao(
            tipo="NOTAS_HARDNESS",
            funcao_sync=sincronizar_desde_ultimo_registro,
            origem="AGENDADOR_AUTO",
            usuario="Scheduler (30m)",
        )
    except Exception as e:
        print(f"❌ Erro no agendamento de notas do Hardness: {e}")
    try:
        registrar_execucao_sincronizacao(
            tipo="ESTOQUE_HARDNESS",
            funcao_sync=lambda: sincronizar_estoque_e_itens_rapido(dias_retroativos_padrao=60),
            origem="AGENDADOR_AUTO",
            usuario="Scheduler (30m)",
        )
    except Exception as e:
        print(f"❌ Erro no agendamento de estoque do Hardness: {e}")


# 2. Fecha conexões antigas do banco antes de atualizar metas
@util.close_old_connections
def tarefa_sync_metas():
    """Roda a sincronização de metas do PipeRun."""
    print("\n⏰ [Scheduler] Executando sync_goals...")
    try:
        registrar_execucao_sincronizacao(
            tipo="METAS_PIPERUN",
            funcao_sync=lambda: sincronizar_metas_piperun(forcar_api=True),
            origem="AGENDADOR_AUTO",
            usuario="Scheduler (24h)",
        )
    except Exception as e:
        print(f"❌ Erro no agendamento de metas: {e}")


@util.close_old_connections
def delete_old_job_executions(max_age=604_800):
    """Limpa o log de execuções antigas a cada semana."""
    DjangoJobExecution.objects.delete_old_job_executions(max_age)


import atexit
from datetime import timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from django.utils import timezone
from sales.models import LogSincronizacao

_scheduler_instance = None


def _calcular_proxima_execucao(tipo_log: str, intervalo: timedelta, atraso_inicial_seg: int = 15):
    """
    Evita disparar sincronização repetida a cada reload do runserver se já houve
    uma sincronização bem-sucedida dentro da janela do intervalo.
    """
    agora = timezone.now()
    try:
        ultimo = (
            LogSincronizacao.objects.filter(tipo=tipo_log, status="SUCESSO")
            .order_by("-iniciado_em")
            .first()
        )
        if ultimo and ultimo.iniciado_em:
            proxima = ultimo.iniciado_em + intervalo
            if proxima > agora:
                return proxima
    except Exception:
        pass
    return agora + timedelta(seconds=atraso_inicial_seg)


def configurar_jobs(scheduler, imediato: bool = False):
    """Registra os jobs periódicos de Notas+Estoque (30m), Metas PipeRun (24h) e limpeza semanal."""
    agora = timezone.now()
    prox_hardness = (
        agora if imediato else _calcular_proxima_execucao("NOTAS_HARDNESS", timedelta(minutes=30), atraso_inicial_seg=15)
    )
    prox_metas = (
        agora if imediato else _calcular_proxima_execucao("METAS_PIPERUN", timedelta(hours=24), atraso_inicial_seg=45)
    )

    # 1. Notas e Estoque do Hardness: a cada 30 minutos
    scheduler.add_job(
        tarefa_sync_hardness,
        trigger=IntervalTrigger(minutes=30),
        id="sync_hardness_job",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=prox_hardness,
    )

    # 2. Metas do PipeRun: a cada 24 horas
    scheduler.add_job(
        tarefa_sync_metas,
        trigger=IntervalTrigger(hours=24),
        id="sync_goals_job",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=prox_metas,
    )

    # 3. Limpeza de logs aos domingos à meia-noite
    scheduler.add_job(
        delete_old_job_executions,
        trigger=CronTrigger(day_of_week="sun", hour="00", minute="00"),
        id="delete_old_job_executions",
        max_instances=1,
        replace_existing=True,
    )


def iniciar_scheduler_background():
    """
    Inicia o agendador em segundo plano (daemon thread) atrelado ao processo do Django.
    Quando o servidor é parado (Ctrl+C), a thread daemon e o hook atexit encerram o agendador junto.
    """
    global _scheduler_instance
    if _scheduler_instance is not None and _scheduler_instance.running:
        return _scheduler_instance

    scheduler = BackgroundScheduler(timezone=settings.TIME_ZONE, daemon=True)
    scheduler.add_jobstore(DjangoJobStore(), "default")
    configurar_jobs(scheduler, imediato=False)
    scheduler.start()
    _scheduler_instance = scheduler

    def _encerrar_scheduler():
        try:
            if scheduler.running:
                scheduler.shutdown(wait=False)
                print("🛑 [Scheduler] Agendador em background encerrado junto com o servidor.")
        except Exception:
            pass

    atexit.register(_encerrar_scheduler)
    print(
        "🚀 [Scheduler] Agendador em background ativo! "
        "(Hardness Notas/Estoque: 30m | PipeRun Metas: 24h)"
    )
    return scheduler


class Command(BaseCommand):
    help = "Inicia o agendador de tarefas periódicas do sistema (Hardness e PipeRun)"

    def handle(self, *args, **options):
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")
        configurar_jobs(scheduler, imediato=True)

        self.stdout.write(self.style.SUCCESS("🚀 Agendador iniciado com sucesso!"))
        self.stdout.write(" - Notas & Estoque Hardness: rodando agora e a cada 30 min")
        self.stdout.write(" - Metas PipeRun: rodando agora e a cada 24 horas\n")

        try:
            scheduler.start()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Agendador interrompido."))
            scheduler.shutdown()