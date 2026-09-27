from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.models import User
from inventory.admin import CertificadoAprovacaoAdmin
from inventory.models import Categoria, CertificadoAprovacao, ItemVenda, ProdutoEPI
from sales.dashboard_services import (
    consolidar_clientes_por_documento,
    gerar_analise_clientes,
    gerar_metricas_e_graficos,
)
from integrations.hardness import HardnessAPI
from sales.models import ContaPagar, ContaReceber, MetaVendedor, NotaFiscal, Orcamento, Vendedor
from sales.services import (
    parse_decimal,
    sincronizar_contas_pagar_hardness,
    sincronizar_contas_receber_hardness,
    sincronizar_metas_piperun,
    sincronizar_orcamentos_hardness,
)
from sales.views import _obter_redirect_seguro


class ParseDecimalTests(TestCase):
    def test_parse_decimal_handles_brazilian_and_iso_formats(self):
        self.assertEqual(parse_decimal("R$ 1.234,56"), Decimal("1234.56"))
        self.assertEqual(parse_decimal("1234,56"), Decimal("1234.56"))
        self.assertEqual(parse_decimal("159.80"), Decimal("159.80"))
        self.assertEqual(parse_decimal(1234.56), Decimal("1234.56"))
        self.assertEqual(parse_decimal(None), Decimal("0.00"))
        self.assertEqual(parse_decimal(""), Decimal("0.00"))


class StabilizationAndBugFixTests(TestCase):
    def test_notafiscal_allows_same_invoice_number_for_different_companies(self):
        nf1 = NotaFiscal.objects.create(
            empresa="AMM EPIS",
            numero_nota="1050",
            cliente_nome="CLIENTE A",
            data_emissao=date(2026, 3, 10),
            valor_total=Decimal("500.00"),
            vendedor_nome="VENDEDORA 1",
            status="EMITIDA",
        )
        nf2 = NotaFiscal.objects.create(
            empresa="AMM SOLUCOES",
            numero_nota="1050",
            cliente_nome="CLIENTE B",
            data_emissao=date(2026, 3, 11),
            valor_total=Decimal("800.00"),
            vendedor_nome="VENDEDORA 2",
            status="EMITIDA",
        )
        self.assertNotEqual(nf1.pk, nf2.pk)
        self.assertEqual(NotaFiscal.objects.filter(numero_nota="1050").count(), 2)

    def test_certificado_aprovacao_admin_badge_handles_none_validity_date(self):
        ca_sem_data = CertificadoAprovacao.objects.create(
            numero_ca="99999",
            data_validade=None,
            status="NAO_ENCONTRADO",
        )
        admin_inst = CertificadoAprovacaoAdmin(CertificadoAprovacao, AdminSite())
        badge_html = str(admin_inst.badge_validade(ca_sem_data))
        self.assertIn("Sem data / Pendente", badge_html)

    @patch("sales.services.PipeRunAPI")
    def test_sincronizar_metas_piperun_accepts_forcar_api_kwarg(self, mock_api_cls):
        mock_api = mock_api_cls.return_value
        mock_api.get_users.return_value = {"data": [{"id": 10, "name": "ALINE"}]}
        mock_api.get_goals.return_value = {
            "data": [{"id": 100, "title": "Meta Comercial", "start_at": "2026-03-01"}]
        }
        mock_api.get_goal_stats.return_value = {
            "data": {"processed": [{"byUser": {"user_id": 10, "value": "50000.00"}}]}
        }
        criadas, atualizadas = sincronizar_metas_piperun(forcar_api=True)
        self.assertEqual(criadas, 1)
        self.assertEqual(atualizadas, 0)
        self.assertTrue(
            MetaVendedor.objects.filter(
                vendedor_nome="ALINE", ano=2026, mes=3
            ).exists()
        )

    def test_open_redirect_protection_rejects_external_hosts(self):
        factory = RequestFactory()
        request = factory.post(
            "/sales/sincronizacao/disparar/",
            {"next": "https://evil.example.com/phish"},
            HTTP_HOST="localhost:8000",
        )
        self.assertEqual(
            _obter_redirect_seguro(request, "/sales/sincronizacao/"),
            "/sales/sincronizacao/",
        )

        request_local = factory.post(
            "/sales/sincronizacao/disparar/",
            {"next": "/inventory/dashboard/?dias=60"},
            HTTP_HOST="localhost:8000",
        )
        self.assertEqual(
            _obter_redirect_seguro(request_local, "/sales/sincronizacao/"),
            "/inventory/dashboard/?dias=60",
        )

    def test_inventory_mutation_views_reject_unauthorized_vendedor_role(self):
        vendedor_user = User.objects.create_user(
            username="vend_teste",
            password="123",
            role=User.Role.VENDEDOR,
        )
        cat = Categoria.objects.create(nome="LUVAS")
        prod = ProdutoEPI.objects.create(
            sku="EPI-001",
            nome="LUVA NITRILICA",
            categoria=cat,
            estoque_minimo=5,
            item_critico=False,
        )

        self.client.force_login(vendedor_user)
        resp = self.client.post(
            reverse("atualizar_parametros_produto"),
            {"sku": "EPI-001", "estoque_minimo": 999, "item_critico": "on"},
        )
        self.assertEqual(resp.status_code, 302)
        prod.refresh_from_db()
        self.assertEqual(prod.estoque_minimo, 5)
        self.assertFalse(prod.item_critico)


class VectorizedDashboardServicesTests(TestCase):
    def setUp(self):
        self.hoje = date.today()
        Vendedor.objects.create(nome_hardness="ALINE", ativo=True)
        cat = Categoria.objects.create(nome="CALCADOS")
        self.prod = ProdutoEPI.objects.create(
            sku="BOTA-01",
            nome="BOTINA SEGURANCA",
            categoria=cat,
            preco_custo=Decimal("40.00"),
            preco_venda=Decimal("100.00"),
        )
        ItemVenda.objects.create(
            id_item_erp="AMM EPIS-2001-1",
            empresa="AMM EPIS",
            numero_nota="2001",
            produto=self.prod,
            codigo_produto="BOTA-01",
            descricao_produto="BOTINA SEGURANCA",
            quantidade=Decimal("2.00"),
            valor_unitario=Decimal("100.00"),
            valor_total=Decimal("200.00"),
            valor_custo_unitario=Decimal("40.00"),
            data_emissao=self.hoje,
            cliente_nome="INDUSTRIA ALPHA LTDA",
            vendedor_nome="ALINE",
        )

    def test_consolidar_clientes_por_documento_unifies_names_by_cnpj(self):
        df = pd.DataFrame(
            [
                {
                    "numero_nota": "1",
                    "cliente_nome": "ALPHA COMERCIO ANTIGO",
                    "cliente_documento": "12.345.678/0001-99",
                    "data_emissao": self.hoje - timedelta(days=40),
                    "valor_total": Decimal("1000.00"),
                    "vendedor_nome": "ALINE",
                },
                {
                    "numero_nota": "2",
                    "cliente_nome": "ALPHA COMERCIO ATUAL LTDA",
                    "cliente_documento": "12345678000199",
                    "data_emissao": self.hoje,
                    "valor_total": Decimal("1500.00"),
                    "vendedor_nome": "ALINE",
                },
            ]
        )
        consolidado = consolidar_clientes_por_documento(df)
        self.assertEqual(list(consolidado["cliente_nome"].unique()), ["ALPHA COMERCIO ATUAL LTDA"])

    def test_gerar_metricas_e_graficos_includes_custo_total_mes_and_supports_skip_charts(self):
        df = pd.DataFrame(
            [
                {
                    "numero_nota": "2001",
                    "cliente_nome": "INDUSTRIA ALPHA LTDA",
                    "cliente_documento": "12345678000199",
                    "data_emissao": self.hoje,
                    "valor_total": Decimal("200.00"),
                    "vendedor_nome": "ALINE",
                }
            ]
        )
        metricas, g1, g2, g3, g4, g5, tabela = gerar_metricas_e_graficos(
            df=df,
            vendedora_selecionada="EMPRESA",
            mes_selecionado=self.hoje.month,
            ano_selecionado=self.hoje.year,
            incluir_margem_admin=True,
            gerar_graficos=False,
        )
        self.assertEqual(metricas["custo_total_mes"], "R$ 80.00")
        self.assertEqual(metricas["margem_bruta_valor"], "R$ 120.00")
        self.assertEqual(g1, "")
        self.assertEqual(len(tabela), 1)

    def test_gerar_analise_clientes_returns_kpis_and_respects_gerar_graficos_flag(self):
        df = pd.DataFrame(
            [
                {
                    "cliente_nome": "INDUSTRIA ALPHA LTDA",
                    "cliente_documento": "12345678000199",
                    "vendedor_nome": "ALINE",
                    "data_emissao": self.hoje,
                    "valor_total": Decimal("5000.00"),
                }
            ]
        )
        kpis, g_status, g_matriz, df_tab = gerar_analise_clientes(
            df=df,
            vendedora_selecionada="EMPRESA",
            filtros_curva=["AA", "A", "B", "C", "D"],
            filtros_status=["Novo", "Ativo", "Em Risco", "Inativo"],
            gerar_graficos=False,
        )
        self.assertEqual(kpis["total_clientes"], 1)
        self.assertEqual(g_status, "")
        self.assertEqual(g_matriz, "")
        self.assertEqual(len(df_tab), 1)


class FinanceiroAndOrcamentosSyncTests(TestCase):
    def test_hardness_api_grid_ids_and_div_root_inference(self):
        api = HardnessAPI(verbose=False)
        self.assertEqual(
            api.grid_dicts[api.contas_receber_url],
            "9564d735f4fc3dc6cd7486755c085be5",
        )
        self.assertEqual(
            api.grid_dicts[api.contas_pagar_url],
            "5ccc4ad192b358516a04e894b47347c5",
        )
        self.assertEqual(
            api.grid_dicts[api.orcamentos_url],
            "ab2f635e63cb22b6a470bf9b1b114731",
        )
        self.assertEqual(api._inferir_div_id_root(api.contas_receber_url), "fin001")
        self.assertEqual(api._inferir_div_id_root(api.contas_pagar_url), "fin002")
        self.assertEqual(api._inferir_div_id_root(api.orcamentos_url), "crm001")

    def test_sincronizar_contas_receber_pagar_e_orcamentos(self):
        class FakeHardnessAPI:
            autenticado = True
            contas_receber_url = "https://example.com/fin/fin001/grid/fin001grid01/"
            contas_pagar_url = "https://example.com/fin/fin002/grid/fin002grid01/"
            orcamentos_url = "https://example.com/crm/crm001/grid/crm001GridPrincipalOrcamentos/"
            empresas_dict = {"AMM EPIS": {"id_sistema": "1"}}

            def trocar_empresa(self, emp_id):
                return True

            def filtrar_contas_receber(self, **kwargs):
                return True

            def filtrar_contas_pagar(self, **kwargs):
                return True

            def filtrar_orcamentos(self, **kwargs):
                return True

            def get_dados(self, url=None):
                if url == self.contas_receber_url:
                    return pd.DataFrame(
                        [
                            {
                                "T002_Id": "9001",
                                "T002_Numero_Documento": "5500",
                                "T002_Numero_Duplicata": "5500-1",
                                "Parcelas": "1 de 1",
                                "T002_D024_Id": "10",
                                "D024_Nome_Fantasia": "CLIENTE TESTE CR",
                                "D024_Cnpj": "12345678000199",
                                "C007_Nome": "ALINE",
                                "T002_Data_Emissao": "2026-09-01",
                                "T002_Data_Vencimento": "2026-09-15",
                                "T002_Data_Recebimento": "2026-09-14",
                                "T002_Valor_Duplicata": "1500.00",
                                "T002_Valor_Total": "1500.00",
                                "T002_Valor_Recebido": "1500.00",
                                "T002_Valor_Saldo": "0.00",
                                "T002_Flag_Cancelada": "N",
                                "T002_Flag_Status": "2",
                            }
                        ]
                    )
                if url == self.contas_pagar_url:
                    return pd.DataFrame(
                        [
                            {
                                "T015_Id": "7001",
                                "T015_Numero_Documento": "NF-88",
                                "T015_Numero_Duplicata": "88/1",
                                "Parcelas": "1/1",
                                "T015_D024_Id": "99",
                                "D024_Nome_Empresa": "FORNECEDOR EPI LTDA",
                                "concat(D024_Cnpj,D024_Cpf)": "98765432000100",
                                "T015_Data_Emissao": "2026-09-01",
                                "T015_Data_Vencimento": "2026-12-30",
                                "T015_Data_Pagamento": None,
                                "T015_Valor_Duplicata": "850.50",
                                "T015_Valor_Total": "850.50",
                                "T015_Valor_Pago": "0.00",
                                "T015_Valor_Saldo": "850.50",
                                "T015_Flag_Cancelada": "N",
                            }
                        ]
                    )
                if url == self.orcamentos_url:
                    return pd.DataFrame(
                        [
                            {
                                "T003_Id": "12345",
                                "T003_Data_Emissao": "2026-09-20",
                                "T003A_Hora_Inclusao": "14:30:00",
                                "T003_D024_Id": "10",
                                "D024_Nome_Fantasia": "CLIENTE ORCAMENTO",
                                "Vendedor.C007_Primeiro_Nome": "ALINE",
                                "T003_Valor_Total_Produtos": "3200.00",
                                "T003_Valor_Total": "3200.00",
                                "T003_Valor_Pendente": "0.00",
                                "Total_Valor_Custo": "1800.00",
                                "T003_Percentual_Margem": "43.75",
                                "T003_IPV": "1.7778",
                                "T003_Flag_Status_Orcamento": "F",
                                "T003_Flag_Perdido": "F",
                                "Pedido": "PED-999",
                                "NF": "5500",
                            }
                        ]
                    )
                return pd.DataFrame()

        fake_api = FakeHardnessAPI()
        cr_c, cr_a = sincronizar_contas_receber_hardness("01/09/2026", "30/09/2026", api=fake_api)
        cp_c, cp_a = sincronizar_contas_pagar_hardness("01/09/2026", "30/09/2026", api=fake_api)
        orc_c, orc_a = sincronizar_orcamentos_hardness("01/09/2026", "30/09/2026", api=fake_api)

        self.assertEqual((cr_c, cr_a), (1, 0))
        self.assertEqual((cp_c, cp_a), (1, 0))
        self.assertEqual((orc_c, orc_a), (1, 0))

        cr = ContaReceber.objects.get(empresa="AMM EPIS", id_titulo_erp="9001")
        self.assertEqual(cr.status, "RECEBIDO")
        self.assertEqual(cr.valor_total, Decimal("1500.00"))

        cp = ContaPagar.objects.get(empresa="AMM EPIS", id_titulo_erp="7001")
        self.assertEqual(cp.status, "A_VENCER")
        self.assertEqual(cp.valor_saldo, Decimal("850.50"))

        orc = Orcamento.objects.get(empresa="AMM EPIS", numero_orcamento="12345")
        self.assertEqual(orc.status, "FINALIZADO")
        self.assertEqual(orc.valor_total, Decimal("3200.00"))
        self.assertEqual(orc.ipv, Decimal("1.7778"))

