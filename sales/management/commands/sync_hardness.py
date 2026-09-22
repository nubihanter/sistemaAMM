from django.core.management.base import BaseCommand
from sales.services import sincronizar_notas_hardness

class Command(BaseCommand):
    help = "Sincroniza notas fiscais direto do Hardness para o banco de dados"

    def add_arguments(self, parser):
        parser.add_argument("--inicio", type=str, default="", help="Data início (DD/MM/AAAA)")
        parser.add_argument("--fim", type=str, default="", help="Data fim (DD/MM/AAAA)")
        parser.add_argument("--empresa", type=str, default="AMM EPIS", help="Nome da empresa cadastrada")

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