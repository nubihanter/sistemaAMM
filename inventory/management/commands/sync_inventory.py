from datetime import datetime
from django.core.management.base import BaseCommand
from inventory.services import (
    sincronizar_estoque_hardness,
    sincronizar_itens_venda_hardness,
    sincronizar_estoque_e_itens_rapido,
)


class Command(BaseCommand):
    help = "Sincroniza o estoque de EPIs e o histórico de itens vendidos do Hardness ERP"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dias",
            type=int,
            default=180,
            help="Quantidade de dias retroativos caso a base de itens ainda esteja vazia (padrão: 180)",
        )
        parser.add_argument(
            "--inicio",
            type=str,
            default="",
            help="Data inicial manual no formato DD/MM/AAAA para sincronizar itens vendidos",
        )
        parser.add_argument(
            "--fim",
            type=str,
            default="",
            help="Data final manual no formato DD/MM/AAAA",
        )
        parser.add_argument(
            "--apenas-estoque",
            action="store_true",
            help="Sincroniza apenas o cadastro e saldo de estoque sem buscar itens de notas",
        )

    def handle(self, *args, **options):
        if options["apenas_estoque"]:
            self.stdout.write(self.style.NOTICE("📦 Sincronizando apenas saldos de estoque no Hardness..."))
            c, a = sincronizar_estoque_hardness()
            self.stdout.write(self.style.SUCCESS(f"✅ Estoque concluído! Criados: {c} | Atualizados: {a}"))
            return

        if options["inicio"]:
            c_est, a_est = sincronizar_estoque_hardness()
            data_fim = options["fim"] or datetime.now().strftime("%d/%m/%Y")
            c_it, a_it = sincronizar_itens_venda_hardness(
                data_inicio=options["inicio"],
                data_fim=data_fim,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ Sincronização manual concluída! Estoque ({c_est} novos, {a_est} atualizados) | "
                    f"Itens de Venda ({c_it} novos, {a_it} atualizados)"
                )
            )
        else:
            res = sincronizar_estoque_e_itens_rapido(dias_retroativos_padrao=options["dias"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ Sincronização concluída! Estoque: {res['estoque_criados']} novos / {res['estoque_atualizados']} atualizados | "
                    f"Itens Vendidos: {res['itens_criados']} novos / {res['itens_atualizados']} atualizados"
                )
            )
