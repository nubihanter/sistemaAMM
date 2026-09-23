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


# 1. Fecha conexões antigas do banco antes de cada execução
@util.close_old_connections
def tarefa_sync_hardness():
    """Roda a sincronização rápida (último registro até hoje)."""
    print("\n⏰ [Scheduler] Executando sync_hardness rápida...")
    try:
        # Chama sem argumentos -> cai na busca do último registro até hoje
        call_command("sync_hardness")
    except Exception as e:
        print(f"❌ Erro no agendamento do Hardness: {e}")


# 2. Fecha conexões antigas do banco antes de atualizar metas
@util.close_old_connections
def tarefa_sync_metas():
    """Roda a sincronização de metas do PipeRun."""
    print("\n⏰ [Scheduler] Executando sync_goals...")
    try:
        call_command("sync_goals")
    except Exception as e:
        print(f"❌ Erro no agendamento de metas: {e}")


@util.close_old_connections
def delete_old_job_executions(max_age=604_800):
    """Limpa o log de execuções antigas a cada semana."""
    DjangoJobExecution.objects.delete_old_job_executions(max_age)


class Command(BaseCommand):
    help = "Inicia o agendador de tarefas periódicas do sistema (Hardness e PipeRun)"

    def handle(self, *args, **options):
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")

        # 1. Notas do Hardness: roda imediatamente e depois a cada 30 minutos
        scheduler.add_job(
            tarefa_sync_hardness,
            trigger=IntervalTrigger(minutes=30),
            id="sync_hardness_job",
            max_instances=1,
            replace_existing=True,
            next_run_time=datetime.now(),
        )

        # 2. Metas do PipeRun: roda imediatamente e depois a cada 24 horas
        scheduler.add_job(
            tarefa_sync_metas,
            trigger=IntervalTrigger(hours=24),
            id="sync_goals_job",
            max_instances=1,
            replace_existing=True,
            next_run_time=datetime.now(),
        )

        # 3. Limpeza de logs aos domingos à meia-noite
        scheduler.add_job(
            delete_old_job_executions,
            trigger=CronTrigger(day_of_week="sun", hour="00", minute="00"),
            id="delete_old_job_executions",
            max_instances=1,
            replace_existing=True,
        )

        self.stdout.write(self.style.SUCCESS("🚀 Agendador iniciado com sucesso!"))
        self.stdout.write(" - Notas Hardness: rodando agora e a cada 30 min (rápido)")
        self.stdout.write(" - Metas PipeRun: rodando agora e a cada 24 horas\n")

        try:
            scheduler.start()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Agendador interrompido."))
            scheduler.shutdown()