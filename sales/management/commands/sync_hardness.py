from datetime import datetime
from django.core.management.base import BaseCommand
from sales.services import (
    registrar_execucao_sincronizacao,
    sincronizar_desde_ultimo_registro,
    sincronizar_financeiro_hardness,
    sincronizar_notas_hardness,
    sincronizar_orcamentos_desde_ultimo_registro,
    sincronizar_orcamentos_hardness,
)


class Command(BaseCommand):
    help = "Sincroniza dados do Hardness ERP (Notas Fiscais, Financeiro CR/CP e Orçamentos CRM)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--modulo",
            type=str,
            default="notas",
            choices=["notas", "financeiro", "orcamentos", "todos"],
            help="Módulo do Hardness a sincronizar: notas (padrão), financeiro, orcamentos ou todos",
        )
        parser.add_argument(
            "--tudo",
            action="store_true",
            help="Força a busca de todo o histórico sem filtro de data inicial (demorado)",
        )
        parser.add_argument(
            "--inicio",
            type=str,
            default="",
            help="Data início manual no formato DD/MM/AAAA",
        )
        parser.add_argument(
            "--fim",
            type=str,
            default="",
            help="Data fim manual no formato DD/MM/AAAA",
        )
        parser.add_argument(
            "--empresa",
            type=str,
            default="TODAS",
            help="Nome da empresa ou 'TODAS'",
        )

    def handle(self, *args, **options):
        empresa = options["empresa"]
        modulo = options["modulo"]
        data_fim = options["fim"] or datetime.now().strftime("%d/%m/%Y")

        if modulo in ("notas", "todos"):
            if options["tudo"]:
                self.stdout.write(self.style.WARNING("⚠️ [Notas] Buscando todo o histórico no Hardness..."))
                _, (criadas, atualizadas) = registrar_execucao_sincronizacao(
                    tipo="NOTAS_HARDNESS",
                    funcao_sync=lambda: sincronizar_notas_hardness(
                        data_inicio="",
                        data_fim=data_fim,
                        empresa_nome=empresa,
                    ),
                    origem="CLI",
                    usuario="Terminal (--tudo)",
                )
            elif options["inicio"]:
                self.stdout.write(self.style.NOTICE(f"[Notas] Período manual: {options['inicio']} até {data_fim}..."))
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
            else:
                self.stdout.write(self.style.NOTICE("🔍 [Notas] Sincronização rápida desde o último registro..."))
                _, (criadas, atualizadas) = registrar_execucao_sincronizacao(
                    tipo="NOTAS_HARDNESS",
                    funcao_sync=lambda: sincronizar_desde_ultimo_registro(empresa_nome=empresa),
                    origem="CLI",
                    usuario="Terminal (rápido)",
                )
            self.stdout.write(self.style.SUCCESS(f"✅ Notas Fiscais: +{criadas} criadas | {atualizadas} atualizadas"))

        if modulo in ("financeiro", "todos"):
            inicio_fin = "" if options["tudo"] else (options["inicio"] or None)
            self.stdout.write(self.style.NOTICE("💰 [Financeiro] Sincronizando Contas a Receber e Contas a Pagar..."))
            _, res_fin = registrar_execucao_sincronizacao(
                tipo="FINANCEIRO_HARDNESS",
                funcao_sync=lambda: sincronizar_financeiro_hardness(
                    data_inicio=inicio_fin,
                    data_fim=data_fim,
                    empresa_nome=empresa,
                ),
                origem="CLI",
                usuario="Terminal",
            )
            self.stdout.write(self.style.SUCCESS(f"✅ Financeiro: {res_fin['mensagem']}"))

        if modulo in ("orcamentos", "todos"):
            self.stdout.write(self.style.NOTICE("📋 [Orçamentos] Sincronizando Orçamentos do CRM..."))
            if options["tudo"] or options["inicio"]:
                _, (orc_c, orc_a) = registrar_execucao_sincronizacao(
                    tipo="ORCAMENTOS_HARDNESS",
                    funcao_sync=lambda: sincronizar_orcamentos_hardness(
                        data_inicio="" if options["tudo"] else options["inicio"],
                        data_fim=data_fim,
                        empresa_nome=empresa,
                    ),
                    origem="CLI",
                    usuario="Terminal",
                )
            else:
                _, (orc_c, orc_a) = registrar_execucao_sincronizacao(
                    tipo="ORCAMENTOS_HARDNESS",
                    funcao_sync=lambda: sincronizar_orcamentos_desde_ultimo_registro(empresa_nome=empresa),
                    origem="CLI",
                    usuario="Terminal (rápido)",
                )
            self.stdout.write(self.style.SUCCESS(f"✅ Orçamentos: +{orc_c} criados | {orc_a} atualizados"))