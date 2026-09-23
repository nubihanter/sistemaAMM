from django.core.management.base import BaseCommand
from sales.services import sincronizar_notas_hardness

class Command(BaseCommand):
    help = "Sincroniza notas fiscais direto do Hardness para o banco de dados"

    # No sales/management/commands/sync_hardness.py

    def add_arguments(self, parser):
        parser.add_argument("--inicio", type=str, default="", help="Data início manual (DD/MM/AAAA)")
        parser.add_argument("--fim", type=str, default="", help="Data fim manual (DD/MM/AAAA)")
        parser.add_argument("--empresa", type=str, default="TODAS", help="Nome da empresa ou 'TODAS' para varrer todas")
        parser.add_argument("--tudo", action="store_true", help="Atualiza todo o histórico de dados")
        parser.add_argument("--loop", action="store_true", help="Mantém o script rodando a cada 30 minutos")

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Iniciando sincronização com o Hardness..."))
        
        criadas, atualizadas = sincronizar_notas_hardness(
            data_inicio=options["inicio"],
            data_fim=options["fim"],
            empresa_nome=options["empresa"]
        )
        
        self.stdout.write(
            self.style.SUCCESS(
                f"Sincronização concluída! Criadas: {criadas} | Atualizadas: {atualizadas}"
            )
        )