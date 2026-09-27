from django.core.management.base import BaseCommand
from inventory.services import sincronizar_vencimentos_ca


class Command(BaseCommand):
    help = (
        "Consulta e atualiza a data de validade e status dos Certificados de Aprovação (CA) no model CertificadoAprovacao. "
        "Por padrão, busca somente os CAs sem data de vencimento cadastrada. "
        "Use --force (ou --all) para forçar a atualização geral de todos os CAs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            "--all",
            action="store_true",
            dest="force",
            help="Força a atualização geral de todos os CAs cadastrados (mesmo os que já possuem data de validade).",
        )
        parser.add_argument(
            "--ca",
            type=str,
            default=None,
            help="Número de um CA específico para consultar/atualizar (ex: --ca 46381).",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=6,
            help="Número de threads simultâneas para consulta (padrão: 6).",
        )

    def handle(self, *args, **options):
        forcar = options.get("force", False)
        ca_especifico = options.get("ca")
        workers = options.get("workers", 6)

        if ca_especifico:
            self.stdout.write(
                self.style.WARNING(f"🚀 Iniciando consulta do CA específico: {ca_especifico}...")
            )
        elif forcar:
            self.stdout.write(
                self.style.WARNING(
                    "🚀 Iniciando atualização GERAL (forçada) de todos os CAs cadastrados..."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "🚀 Iniciando consulta apenas dos CAs SEM data de vencimento cadastrada..."
                )
            )

        res = sincronizar_vencimentos_ca(
            forcar_todos=forcar,
            numero_ca_especifico=ca_especifico,
            max_workers=workers,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"🎉 Processo finalizado! Total processados: {res['total']} | "
                f"Atualizados: {res['atualizados']} | Vencidos: {res['vencidos']} | "
                f"Não encontrados: {res['nao_encontrados']}"
            )
        )
