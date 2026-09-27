from datetime import datetime
from django.core.management.base import BaseCommand
from sales.services import (
    registrar_execucao_sincronizacao,
    sincronizar_notas_hardness,
    sincronizar_desde_ultimo_registro,
)


class Command(BaseCommand):
    help = "Sincroniza notas fiscais do Hardness (por padrão apenas do último registro até hoje)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tudo",
            action="store_true",
            help="Força a busca de todo o histórico sem filtro de data inicial (demorado)"
        )
        parser.add_argument(
            "--inicio",
            type=str,
            default="",
            help="Data início manual no formato DD/MM/AAAA"
        )
        parser.add_argument(
            "--fim",
            type=str,
            default="",
            help="Data fim manual no formato DD/MM/AAAA"
        )
        parser.add_argument(
            "--empresa",
            type=str,
            default="TODAS",
            help="Nome da empresa ou 'TODAS'"
        )

    def handle(self, *args, **options):
        empresa = options["empresa"]

        # 1. Se passou --tudo explicitamente, traz o histórico completo
        if options["tudo"]:
            self.stdout.write(self.style.WARNING("⚠️ Modo COMPLETO ativado: buscando todo o histórico no Hardness..."))
            _, (criadas, atualizadas) = registrar_execucao_sincronizacao(
                tipo="NOTAS_HARDNESS",
                funcao_sync=lambda: sincronizar_notas_hardness(
                    data_inicio="",
                    data_fim=datetime.now().strftime("%d/%m/%Y"),
                    empresa_nome=empresa,
                ),
                origem="CLI",
                usuario="Terminal (--tudo)",
            )

        # 2. Se informou data manual via terminal
        elif options["inicio"]:
            data_fim = options["fim"] or datetime.now().strftime("%d/%m/%Y")
            self.stdout.write(self.style.NOTICE(f"Buscando período manual: {options['inicio']} até {data_fim}..."))
            _, (criadas, atualizadas) = registrar_execucao_sincronizacao(
                tipo="NOTAS_HARDNESS",
                funcao_sync=lambda: sincronizar_notas_hardness(
                    data_inicio=options["inicio"],
                    data_fim=data_fim,
                    empresa_nome=empresa,
                ),
                origem="CLI",
                usuario=f"Terminal ({options['inicio']}-{data_fim})",
            )

        # 3. COMPORTAMENTO PADRÃO (usado pelo scheduler): da última data gravada até hoje
        else:
            self.stdout.write(self.style.NOTICE("🔍 Identificando último registro no banco para sincronização rápida..."))
            _, (criadas, atualizadas) = registrar_execucao_sincronizacao(
                tipo="NOTAS_HARDNESS",
                funcao_sync=lambda: sincronizar_desde_ultimo_registro(empresa_nome=empresa),
                origem="CLI",
                usuario="Terminal (rápido)",
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"✅ Concluído com sucesso! Criadas: {criadas} | Atualizadas: {atualizadas}"
            )
        )