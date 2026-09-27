from django.core.management.base import BaseCommand
from sales.services import registrar_execucao_sincronizacao, sincronizar_metas_piperun


class Command(BaseCommand):
    help = "Sincroniza exclusivamente as metas do PipeRun para a base de dados"

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("🎯 Iniciando sincronização exclusiva de metas do PipeRun..."))
        try:
            _, (criadas, atualizadas) = registrar_execucao_sincronizacao(
                tipo="METAS_PIPERUN",
                funcao_sync=lambda: sincronizar_metas_piperun(forcar_api=True),
                origem="CLI",
                usuario="Terminal",
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ Metas atualizadas com sucesso! Novas: {criadas} | Atualizadas: {atualizadas}"
                )
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Erro ao atualizar metas: {e}"))