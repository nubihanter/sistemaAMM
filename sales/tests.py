from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

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
    gerar_dashboard_orcamentos,
    gerar_metricas_e_graficos,
)
from integrations.hardness import HardnessAPI
from sales.models import ContaPagar, ContaReceber, ItemOrcamento, MetaVendedor, NotaFiscal, Orcamento, Vendedor
from sales.services import (
    parse_decimal,
    sincronizar_contas_pagar_hardness,
    sincronizar_contas_receber_hardness,
    sincronizar_metas_piperun,
    sincronizar_notas_hardness,
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
            orcamentos_produtos_url = "https://example.com/crm/crm001/grid/crm001GridPrincipalOrcamentosProdutos/"
            empresas_dict = {"AMM EPIS": {"id_sistema": "1"}}

            def trocar_empresa(self, emp_id):
                return True

            def filtrar_contas_receber(self, **kwargs):
                return True

            def filtrar_contas_pagar(self, **kwargs):
                return True

            def filtrar_orcamentos(self, **kwargs):
                return True

            def filtrar_orcamentos_produtos(self, **kwargs):
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

    def test_dashboard_orcamentos_and_financeiro_views_and_calculations(self):
        admin_user = User.objects.create_user(
            username="admin_fin",
            password="123",
            role=User.Role.ADMINISTRADOR,
        )
        supervisor_user = User.objects.create_user(
            username="sup_comercial",
            password="123",
            role=User.Role.SUPERVISOR,
        )
        vendedor_user = User.objects.create_user(
            username="ALINE",
            password="123",
            role=User.Role.VENDEDOR,
            nome_vendedor_erp="ALINE",
        )

        Vendedor.objects.update_or_create(
            nome_hardness="ALINE",
            defaults={"ativo": True, "ativo_ranking": True},
        )
        Vendedor.objects.update_or_create(
            nome_hardness="VEND_INATIVO",
            defaults={"ativo": False, "ativo_ranking": False},
        )

        # Cria orçamentos em Set/2026 para ALINE: R$ 3.000 realizado e R$ 1.000 perdido (D047_Id=41) -> 75.0% realizado em R$
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="ORC-1",
            data_emissao=date(2026, 9, 10),
            cliente_nome="CLIENTE GANHO",
            vendedor_nome="ALINE",
            valor_total=Decimal("3000.00"),
            valor_custo=Decimal("1800.00"),
            status="FINALIZADO",
        )
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="ORC-2",
            data_emissao=date(2026, 9, 12),
            cliente_nome="CLIENTE PERDIDO",
            vendedor_nome="ALINE",
            valor_total=Decimal("1000.00"),
            valor_custo=Decimal("600.00"),
            status="PERDIDO",
            flag_perdido="S",
            motivo_perda="Concorrente 15% menor",
            dados_brutos={"T003_D047_Id": "41"},
        )
        # Orçamento de vendedor inativo (não deve entrar no gráfico/ranking de vendedores)
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="ORC-3",
            data_emissao=date(2026, 9, 15),
            cliente_nome="CLIENTE ANTIGO",
            vendedor_nome="VEND_INATIVO",
            valor_total=Decimal("500.00"),
            valor_custo=Decimal("300.00"),
            status="PENDENTE",
        )

        # Cria Contas a Receber e Contas a Pagar em Set/2026
        ContaReceber.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CR-100",
            numero_duplicata="100/1",
            cliente_nome="CLIENTE GANHO",
            data_emissao=date(2026, 9, 10),
            data_vencimento=date(2026, 9, 20),
            data_recebimento=date(2026, 9, 19),
            valor_total=Decimal("3000.00"),
            valor_recebido=Decimal("3000.00"),
            valor_saldo=Decimal("0.00"),
            portador="BOLETO SICREDI",
            status="RECEBIDO",
        )
        ContaPagar.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CP-200",
            numero_duplicata="200/1",
            fornecedor_nome="FORNECEDOR A",
            centro_custo="COMPRAS/ESTOQUE",
            grupo_conta="OPERACIONAL - COMPRA MERCADORIA",
            data_emissao=date(2026, 9, 5),
            data_vencimento=date(2026, 9, 18),
            data_pagamento=date(2026, 9, 18),
            valor_total=Decimal("1200.00"),
            valor_pago=Decimal("1200.00"),
            valor_saldo=Decimal("0.00"),
            portador="BOLETO SICREDI",
            status="PAGO",
        )

        # 1. Testa página de Orçamentos como Vendedor (vê apenas a sua carteira)
        self.client.force_login(vendedor_user)
        resp_orc = self.client.get(reverse("dashboard_orcamentos") + "?mes=9&ano=2026")
        self.assertEqual(resp_orc.status_code, 200)
        self.assertFalse(resp_orc.context["pode_selecionar"])
        self.assertEqual(resp_orc.context["kpis"]["qtd_total"], 2)
        self.assertEqual(resp_orc.context["kpis"]["pct_realizado_valor"], "75.0%")

        # Confirma motivo de perda padronizado (D047_Id=41 -> PREÇO DE CONCORRENTE)
        orc_perdido_row = [r for r in resp_orc.context["tabela_orcamentos"] if r["numero_orcamento"] == "ORC-2"][0]
        self.assertEqual(orc_perdido_row["motivo_perda_padrao"], "PREÇO DE CONCORRENTE")
        self.assertEqual(orc_perdido_row["motivo_perda"], "Concorrente 15% menor")

        # Vendedor não acessa Financeiro (redireciona para dashboard_vendas)
        resp_fin_vend = self.client.get(reverse("dashboard_financeiro"))
        self.assertEqual(resp_fin_vend.status_code, 302)

        # 2. Testa Supervisor: acessa todos os orçamentos (e filtra inativos do gráfico), mas NÃO acessa Financeiro
        self.client.force_login(supervisor_user)
        resp_orc_sup = self.client.get(reverse("dashboard_orcamentos") + "?mes=9&ano=2026")
        self.assertEqual(resp_orc_sup.status_code, 200)
        self.assertTrue(resp_orc_sup.context["pode_selecionar"])
        nomes_grafico = [rv["vendedor_nome"] for rv in resp_orc_sup.context["resumo_vendedores"]]
        self.assertEqual(nomes_grafico, ["ALINE"])
        self.assertNotIn("VEND_INATIVO", nomes_grafico)

        resp_fin_sup = self.client.get(reverse("dashboard_financeiro"))
        self.assertEqual(resp_fin_sup.status_code, 302)

        # 3. Testa exportação Excel de Orçamentos e página Financeiro como Administrador
        self.client.force_login(admin_user)
        resp_excel = self.client.get(reverse("exportar_orcamentos_excel") + "?mes=9&ano=2026")
        self.assertEqual(resp_excel.status_code, 200)
        self.assertIn("spreadsheetml", resp_excel["Content-Type"])

        resp_fin = self.client.get(reverse("dashboard_financeiro") + "?mes=9&ano=2026&regime=CAIXA")
        self.assertEqual(resp_fin.status_code, 200)
        self.assertEqual(resp_fin.context["dre"]["lucro_liquido_raw"], 1800.0)
        self.assertEqual(len(resp_fin.context["resumo_portadores"]), 1)
        self.assertEqual(resp_fin.context["resumo_portadores"][0]["portador"], "BOLETO SICREDI")
        self.assertEqual(resp_fin.context["resumo_portadores"][0]["saldo_liquido_conciliado"], 1800.0)
        self.assertEqual(resp_fin.context["resumo_portadores"][0]["pct_entradas"], 100.0)
        self.assertEqual(resp_fin.context["resumo_portadores"][0]["pct_saidas"], 100.0)
        # Confirma que o gráfico DRE é de cascatas (waterfall) e os gráficos de portador são de pizza (pie)
        self.assertIn('"type": "waterfall"', resp_fin.context["grafico_dre"])
        self.assertIn('"type": "pie"', resp_fin.context["grafico_pizza_entradas"])
        self.assertIn('"type": "pie"', resp_fin.context["grafico_pizza_saidas"])

    def test_disparar_sincronizacao_background_returns_log_first_without_bool_error(self):
        from unittest.mock import MagicMock, patch
        from sales.services import disparar_sincronizacao_background

        with patch("threading.Thread", return_value=MagicMock()):
            log, iniciou = disparar_sincronizacao_background(
                tipo="NOTAS_HARDNESS",
                funcao_sync=lambda: (2, 1),
                origem="MANUAL_PAINEL",
                usuario="admin_test",
            )
            self.assertTrue(iniciou)
            self.assertEqual(log.get_tipo_display(), "Notas Fiscais (Hardness)")

            # Segunda chamada com job em andamento também deve retornar (LogSincronizacao, False)
            log_em_andamento, iniciou_novamente = disparar_sincronizacao_background(
                tipo="NOTAS_HARDNESS",
                funcao_sync=lambda: (2, 1),
                origem="MANUAL_PAINEL",
                usuario="admin_test",
            )
            self.assertFalse(iniciou_novamente)
            self.assertEqual(log_em_andamento.id, log.id)
            self.assertEqual(log_em_andamento.get_tipo_display(), "Notas Fiscais (Hardness)")

    def test_sincronizar_financeiro_hardness_uses_empty_data_fim_by_default(self):
        from unittest.mock import patch
        from sales.services import sincronizar_financeiro_hardness

        chamadas_cr = []
        chamadas_cp = []

        class DummyAPI:
            def login(self):
                return True

        with (
            patch("sales.services.HardnessAPI", return_value=DummyAPI()),
            patch(
                "sales.services.sincronizar_contas_receber_hardness",
                side_effect=lambda **kw: (chamadas_cr.append(kw) or (1, 0)),
            ),
            patch(
                "sales.services.sincronizar_contas_pagar_hardness",
                side_effect=lambda **kw: (chamadas_cp.append(kw) or (1, 0)),
            ),
        ):
            sincronizar_financeiro_hardness()

        self.assertEqual(len(chamadas_cr), 1)
        self.assertEqual(len(chamadas_cp), 1)
        self.assertEqual(chamadas_cr[0]["data_fim"], "")
        self.assertEqual(chamadas_cp[0]["data_fim"], "")

    def test_vendas_prazo_medio_por_vendedor_e_grafico_comparativo_restrito(self):
        admin_user = User.objects.create_user(
            username="admin_prazo",
            password="123",
            role=User.Role.ADMINISTRADOR,
        )
        supervisor_user = User.objects.create_user(
            username="sup_prazo",
            password="123",
            role=User.Role.SUPERVISOR,
        )
        vend_aline = User.objects.create_user(
            username="aline_user",
            password="123",
            role=User.Role.VENDEDOR,
            nome_vendedor_erp="ALINE",
        )

        Vendedor.objects.update_or_create(
            nome_hardness="ALINE",
            defaults={"ativo": True, "ativo_ranking": True},
        )
        Vendedor.objects.update_or_create(
            nome_hardness="BRUNO",
            defaults={"ativo": True, "ativo_ranking": True},
        )

        # ALINE: NF de R$ 1.000 com prazo 28/42 (média 35 dias)
        NotaFiscal.objects.create(
            empresa="AMM EPIS",
            numero_nota="NF-ALINE-1",
            data_emissao=date(2026, 9, 10),
            cliente_nome="CLIENTE ALINE",
            vendedor_nome="ALINE",
            valor_total=Decimal("1000.00"),
            status="AUTORIZADA",
            dados_brutos={"T007_Prazos(T007_Id)": "28/42"},
        )
        # BRUNO: NF de R$ 2.000 com prazo 28 dias
        NotaFiscal.objects.create(
            empresa="AMM EPIS",
            numero_nota="NF-BRUNO-1",
            data_emissao=date(2026, 9, 12),
            cliente_nome="CLIENTE BRUNO",
            vendedor_nome="BRUNO",
            valor_total=Decimal("2000.00"),
            status="AUTORIZADA",
            dados_brutos={"T007_Prazos(T007_Id)": "28"},
        )

        # 1. Vendedor ALINE vê apenas o seu prazo médio (35.0 dias) e NÃO recebe o gráfico comparativo
        self.client.force_login(vend_aline)
        resp_vend = self.client.get(reverse("dashboard_vendas") + "?mes=9&ano=2026")
        self.assertEqual(resp_vend.status_code, 200)
        self.assertEqual(resp_vend.context["metricas"]["prazo_medio_dias"], "35.0 dias")
        self.assertFalse(resp_vend.context["pode_ver_comparativo_prazo"])
        self.assertEqual(resp_vend.context["grafico_prazo_vendedores"], "")

        # 2. Supervisor e Admin recebem o gráfico comparativo entre todos os vendedores ativos
        for usr in (supervisor_user, admin_user):
            self.client.force_login(usr)
            resp_adm = self.client.get(reverse("dashboard_vendas") + "?mes=9&ano=2026")
            self.assertEqual(resp_adm.status_code, 200)
            self.assertTrue(resp_adm.context["pode_ver_comparativo_prazo"])
            self.assertIn("ALINE", resp_adm.context["grafico_prazo_vendedores"])
            self.assertIn("BRUNO", resp_adm.context["grafico_prazo_vendedores"])

    def test_fluxo_caixa_liquidez_inadimplencia_e_gestao_custos(self):
        from sales.models import ClassificacaoCusto, ConfiguracaoFinanceira, ContaPagar, ContaReceber

        admin_user = User.objects.create_user(
            username="gestor_fin",
            first_name="Gestor",
            password="123",
            role=User.Role.ADMINISTRADOR,
        )
        self.client.force_login(admin_user)

        # 1. Atualiza o Saldo Bancário Manual para R$ 100.000,00 e Reserva Mínima para R$ 30.000,00
        resp_post = self.client.post(
            reverse("dashboard_financeiro"),
            {
                "acao_fin": "atualizar_saldo",
                "empresa": "TODAS",
                "saldo_bancario_atual": "100000,00",
                "reserva_minima": "30000,00",
            },
        )
        self.assertEqual(resp_post.status_code, 302)
        cfg = ConfiguracaoFinanceira.objects.get(empresa="TODAS")
        self.assertEqual(float(cfg.saldo_bancario_atual), 100000.0)
        self.assertEqual(float(cfg.reserva_minima), 30000.0)
        self.assertEqual(cfg.usuario_atualizacao, "Gestor")

        # 2. Cria dados de Outubro e Novembro/2026 conforme exemplo da especificação:
        # Outubro: Recebimentos R$ 400.000, Pagamentos R$ 350.000 -> Saldo Projetado = 100k + 400k - 350k = R$ 150.000 (Disponível R$ 120.000)
        # Novembro: Recebimentos R$ 350.000, Pagamentos R$ 482.000 -> Saldo Projetado = 150k + 350k - 482k = R$ 18.000 (Abaixo da reserva de R$ 30k!)
        ContaReceber.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CR-OUT-1",
            cliente_nome="CLIENTE OUTUBRO",
            data_vencimento=date(2026, 10, 15),
            valor_total=Decimal("400000.00"),
            valor_saldo=Decimal("400000.00"),
            status="A_VENCER",
        )
        # Título de Outubro já recebido antecipadamente em Setembro (para testar diagnóstico 234 vs 242)
        ContaReceber.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CR-OUT-ANTEC",
            cliente_nome="CLIENTE ANTECIPADO",
            data_vencimento=date(2026, 10, 20),
            data_recebimento=date(2026, 9, 25),
            valor_total=Decimal("15000.00"),
            valor_recebido=Decimal("15000.00"),
            valor_saldo=Decimal("0.00"),
            status="RECEBIDO",
        )
        # Transferência interna entre empresas (não deve inflar o consolidado)
        ContaReceber.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CR-TRANSF-INT",
            cliente_nome="AMM SOLUCOES EM EPIS LTDA",
            data_vencimento=date(2026, 10, 10),
            valor_total=Decimal("20000.00"),
            valor_saldo=Decimal("20000.00"),
            status="A_VENCER",
        )
        # Título vencido para testar Taxa de Inadimplência
        ContaReceber.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CR-VENC-1",
            cliente_nome="CLIENTE EM ATRASO",
            data_vencimento=date(2026, 9, 10),
            dias_atraso=19,
            valor_total=Decimal("25000.00"),
            valor_saldo=Decimal("25000.00"),
            status="VENCIDO",
        )

        ContaPagar.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CP-OUT-1",
            fornecedor_nome="FORNECEDOR MERCADORIA",
            centro_custo="COMPRAS/ESTOQUE",
            grupo_conta="OPERACIONAL - COMPRA MERCADORIA",
            subconta="COMPRA MERCADORIA",
            data_vencimento=date(2026, 10, 10),
            valor_total=Decimal("350000.00"),
            valor_saldo=Decimal("350000.00"),
            status="A_VENCER",
        )
        ContaReceber.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CR-NOV-1",
            cliente_nome="CLIENTE NOVEMBRO",
            data_vencimento=date(2026, 11, 20),
            valor_total=Decimal("350000.00"),
            valor_saldo=Decimal("350000.00"),
            status="A_VENCER",
        )
        ContaPagar.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="CP-NOV-1",
            fornecedor_nome="FOLHA E FORNECEDORES NOV",
            centro_custo="ADMINISTRATIVO",
            grupo_conta="ADMINSTRATIVO - SALARIOS (ADM)",
            subconta="SALARIOS (ADM)",
            data_vencimento=date(2026, 11, 5),
            valor_total=Decimal("482000.00"),
            valor_saldo=Decimal("482000.00"),
            status="A_VENCER",
        )

        resp = self.client.get(reverse("dashboard_financeiro") + "?mes=10&ano=2026&horizonte=3")
        self.assertEqual(resp.status_code, 200)
        fluxo = resp.context["fluxo"]

        # Verifica projeção cumulativa de Outubro e Novembro
        proj = fluxo["projecao_mensal"]
        self.assertEqual(len(proj), 3)
        self.assertEqual(proj[0]["saldo_inicial_raw"], 100000.0)
        self.assertEqual(proj[0]["recebimentos_previstos_raw"], 400000.0)
        self.assertEqual(proj[0]["pagamentos_previstos_raw"], 350000.0)
        self.assertEqual(proj[0]["saldo_projetado_raw"], 150000.0)
        self.assertEqual(proj[0]["disponivel_raw"], 120000.0)
        self.assertFalse(proj[0]["abaixo_reserva"])

        # Novembro começa com R$ 150.000, recebe R$ 350.000 e paga R$ 482.000 -> R$ 18.000 (abaixo de R$ 30.000!)
        self.assertEqual(proj[1]["saldo_inicial_raw"], 150000.0)
        self.assertEqual(proj[1]["saldo_projetado_raw"], 18000.0)
        self.assertTrue(proj[1]["abaixo_reserva"])
        self.assertEqual(proj[1]["necessidade_adicional_raw"], 12000.0)

        # Verifica que o status geral acusou AÇÃO NECESSÁRIA por causa de Novembro
        self.assertEqual(fluxo["status_geral"]["codigo"], "ACAO_NECESSARIA")

        # Verifica diagnóstico de títulos de Outubro (1 em aberto + 1 já recebido antecipadamente = 2 títulos no vencimento)
        diag_cr = fluxo["diagnostico_titulos_cr"]
        self.assertEqual(diag_cr["qtd_total_vencimento"], 2)
        self.assertEqual(diag_cr["qtd_em_aberto"], 1)
        self.assertEqual(diag_cr["qtd_ja_recebidos"], 1)

        # Verifica que a transferência interna de R$ 20.000 foi neutralizada no consolidado
        self.assertEqual(fluxo["resumo_intercompany"]["qtd_cr"], 1)

        # 3. Testa customização persistente de Classificação de Custo
        cls_sal = ClassificacaoCusto.objects.get(conta_chave="SALARIOS (ADM)")
        resp_cls = self.client.post(
            reverse("dashboard_financeiro"),
            {
                "acao_fin": "salvar_classificacao_custo",
                "empresa": "TODAS",
                "alerta_aumento_pct": "12,0",
                "alerta_aumento_valor": "600,00",
                "conta_id": [str(cls_sal.id)],
                f"categoria_{cls_sal.id}": "Folha Administrativa",
                f"subcategoria_{cls_sal.id}": "Salários Mensais",
                f"tipo_custo_{cls_sal.id}": "FIXO",
                f"recorrente_{cls_sal.id}": "1",
            },
        )
        self.assertEqual(resp_cls.status_code, 302)
        cls_sal.refresh_from_db()
        self.assertEqual(cls_sal.categoria, "Folha Administrativa")
        self.assertTrue(cls_sal.editado_manualmente)

    @patch("sales.services.HardnessAPI")
    def test_sync_notas_cancelamento_e_desduplicacao(self, mock_api_cls):
        """
        Garante que quando uma nota é cancelada no Hardness ERP, ela é atualizada para CANCELADA,
        as contas a receber vinculadas são marcadas como CANCELADO e os itens de venda excluídos.
        """
        # Cria NF ativa inicial, título de CR e item de venda
        nf = NotaFiscal.objects.create(
            empresa="AMM EPIS",
            numero_nota="0010549",
            cliente_nome="TKP IND COM E RENOV IMPLEMENTO",
            data_emissao=date(2026, 9, 29),
            valor_total=Decimal("7720.09"),
            status="Aguard.coleta",
        )
        cr = ContaReceber.objects.create(
            empresa="AMM EPIS",
            id_titulo_erp="9185",
            numero_documento="10549",
            numero_duplicata="10549A",
            cliente_nome="TKP INDUSTRIA",
            data_emissao=date(2026, 9, 29),
            data_vencimento=date(2026, 10, 27),
            valor_total=Decimal("7720.09"),
            status="A_VENCER",
            cancelada=False,
        )
        cat = Categoria.objects.create(nome="LUVAS")
        prod = ProdutoEPI.objects.create(
            sku="667",
            nome="LUVAS DE PROTECAO",
            categoria=cat,
            preco_venda=Decimal("100.00"),
        )
        item_venda = ItemVenda.objects.create(
            id_item_erp="1-8024",
            empresa="AMM EPIS",
            numero_nota="0010549",
            data_emissao=date(2026, 9, 29),
            cliente_nome="TKP IND COM E RENOV IMPLEMENTO",
            produto=prod,
            codigo_produto="667",
            descricao_produto="LUVAS DE PROTECAO",
            quantidade=Decimal("2.00"),
            valor_total=Decimal("200.00"),
        )

        mock_api = mock_api_cls.return_value
        mock_api.empresas_dict = {"AMM EPIS": {"id_sistema": "1"}}
        mock_api.trocar_empresa.return_value = True
        mock_api.filtrar.return_value = True

        # Simula retorno do Hardness com a nota 0010549 cancelada (T007_Flag_Cancelada = 'S')
        mock_api.get_dados.return_value = pd.DataFrame([
            {
                "T007_Id": "9575",
                "T007_Numero_Nota_Fiscal": "0010549",
                "T007_Flag_ACP": "6",
                "D024_Nome_Empresa": "TKP IND COM E RENOV IMPLEMENTO",
                "D024_Id": "2128",
                "T007_Data_Emissao": "2026-09-29",
                "T007_Valor_Total_Produtos": "0.00",
                "D006_Codigo_CFOP": "5.102",
                "T005_Status": "Pendente",
                "T007_Flag_Cancelada": "S",
                "vendedor.C007_Primeiro_Nome": "LUCAS",
            }
        ])

        criadas, atualizadas = sincronizar_notas_hardness(data_inicio="29/09/2026", data_fim="29/09/2026")
        self.assertEqual(atualizadas, 1)

        nf.refresh_from_db()
        self.assertEqual(nf.status, "CANCELADA")

        cr.refresh_from_db()
        self.assertTrue(cr.cancelada)
        self.assertEqual(cr.status, "CANCELADO")

        self.assertFalse(ItemVenda.objects.filter(numero_nota="0010549").exists())

    def test_dashboard_orcamentos_desconsidera_motivos_especificados(self):
        """
        Garante que no dashboard de orçamentos, os motivos duplicado, teste,
        erro de preenchimento e orçamento duplicado não são computados.
        """
        # 1. Orçamento Ganho (deve ser computado)
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="9001",
            data_emissao=date(2026, 9, 10),
            cliente_nome="CLIENTE REAL",
            vendedor_nome="VENDEDOR 1",
            valor_total=Decimal("1000.00"),
            status="FINALIZADO",
            flag_status="F",
            flag_perdido="F",
        )
        # 2. Orçamento Perdido Legítimo (Preço de Concorrente - deve ser computado)
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="9002",
            data_emissao=date(2026, 9, 11),
            cliente_nome="CLIENTE REAL 2",
            vendedor_nome="VENDEDOR 1",
            valor_total=Decimal("500.00"),
            status="PERDIDO",
            flag_perdido="S",
            motivo_perda="Perdemos por preço",
            dados_brutos={"T003_D047_Id": "41"},  # PREÇO DE CONCORRENTE
        )
        # 3. Orçamento Duplicado (D047 = 49 - NÃO DEVE SER COMPUTADO)
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="9003",
            data_emissao=date(2026, 9, 12),
            cliente_nome="CLIENTE TESTE",
            vendedor_nome="VENDEDOR 1",
            valor_total=Decimal("300.00"),
            status="PERDIDO",
            flag_perdido="S",
            dados_brutos={"T003_D047_Id": "49"},  # DUPLICADO
        )
        # 4. Orçamento Teste (D047 = 44 - NÃO DEVE SER COMPUTADO)
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="9004",
            data_emissao=date(2026, 9, 13),
            cliente_nome="TESTE CLIENTE",
            vendedor_nome="VENDEDOR 1",
            valor_total=Decimal("200.00"),
            status="PERDIDO",
            flag_perdido="S",
            motivo_perda="TESTEEEEEEEE",
            dados_brutos={"T003_D047_Id": "44"},  # TESTE DO SISTEMA
        )
        # 5. Erro de Preenchimento (D047 = 40 - NÃO DEVE SER COMPUTADO)
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="9005",
            data_emissao=date(2026, 9, 14),
            cliente_nome="CLIENTE X",
            vendedor_nome="VENDEDOR 1",
            valor_total=Decimal("150.00"),
            status="PERDIDO",
            flag_perdido="S",
            dados_brutos={"T003_D047_Id": "40"},  # ERRO DE PREENCHIMENTO
        )
        # 6. Orçamento Duplicado por texto no motivo (D047 = 0 - NÃO DEVE SER COMPUTADO)
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="9006",
            data_emissao=date(2026, 9, 15),
            cliente_nome="CLIENTE Y",
            vendedor_nome="VENDEDOR 1",
            valor_total=Decimal("400.00"),
            status="PERDIDO",
            flag_perdido="S",
            motivo_perda="orçamento duplicado lancei em outro",
            dados_brutos={"T003_D047_Id": "0"},
        )

        kpis, _, _, _, resumo, tabela, ranking_preco = gerar_dashboard_orcamentos(
            mes_selecionado=9,
            ano_selecionado=2026,
            gerar_graficos=False,
        )

        # Apenas 9001 (ganho R$ 1000) e 9002 (perdido R$ 500) devem ser computados!
        self.assertEqual(kpis["qtd_total"], 2)
        self.assertEqual(kpis["valor_orcado_raw"], 1500.0)
        self.assertEqual(kpis["qtd_realizados"], 1)
        self.assertEqual(kpis["valor_realizado_raw"], 1000.0)
        self.assertEqual(kpis["qtd_perdidos"], 1)
        self.assertEqual(kpis["valor_perdido_raw"], 500.0)
        self.assertNotIn("qtd_desconsiderados", kpis)
        self.assertEqual(len(tabela), 2)
        numeros_na_tabela = [item["numero_orcamento"] for item in tabela]
        self.assertIn("9001", numeros_na_tabela)
        self.assertIn("9002", numeros_na_tabela)
        self.assertNotIn("9003", numeros_na_tabela)
        self.assertNotIn("9004", numeros_na_tabela)
        self.assertNotIn("9005", numeros_na_tabela)
        self.assertNotIn("9006", numeros_na_tabela)

    def test_sincronizacao_orcamento_cancelado_e_desconsiderado(self):
        """
        Verifica se a sincronização com o Hardness define status='CANCELADO' para
        orçamentos com flag_status='C' ou motivos duplicado/teste/erro de preenchimento,
        e atualiza orçamentos que previamente subiram como PENDENTE.
        """
        # Cria um orçamento previamente salvo como PENDENTE no banco
        Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="99901",
            data_emissao=date(2026, 9, 5),
            status="PENDENTE",
            valor_total=Decimal("250.00"),
        )

        fake_api = MagicMock()
        fake_api.autenticado = True
        fake_api.empresas_dict = {"AMM EPIS": {"id_sistema": "1"}}
        fake_api.trocar_empresa.return_value = True
        fake_api.filtrar_orcamentos.return_value = True
        fake_api.filtrar_orcamentos_produtos.return_value = True
        fake_api.orcamentos_produtos_url = "https://example.com/orcamentos_produtos"

        fake_data = [
            # 1. Orçamento que já subiu como PENDENTE e agora foi cancelado com motivo DUPLICADO
            {
                "T003_Id": "99901",
                "T003_Data_Emissao": "2026-09-05",
                "T003_Flag_Status_Orcamento": "C",
                "T003_Flag_Perdido": "S",
                "T003_D047_Id": "49",
                "T003_Observacao_Orcamento_Perdido": "duplicado",
                "T003_Valor_Total": "250.00",
                "Vendedor.C007_Primeiro_Nome": "LUCAS",
                "Status": "Cancelado",
            },
            # 2. Novo orçamento cancelado por motivo comercial legítimo
            {
                "T003_Id": "99902",
                "T003_Data_Emissao": "2026-09-10",
                "T003_Flag_Status_Orcamento": "C",
                "T003_Flag_Perdido": "S",
                "T003_D047_Id": "41",
                "T003_Observacao_Orcamento_Perdido": "perdeu para concorrente",
                "T003_Valor_Total": "800.00",
                "Vendedor.C007_Primeiro_Nome": "LUCAS",
                "Status": "Cancelado",
            },
            # 3. Novo orçamento marcado como TESTE DO SISTEMA (D047 = 44)
            {
                "T003_Id": "99903",
                "T003_Data_Emissao": "2026-09-12",
                "T003_Flag_Status_Orcamento": "P",
                "T003_Flag_Perdido": "S",
                "T003_D047_Id": "44",
                "T003_Observacao_Orcamento_Perdido": "teste",
                "T003_Valor_Total": "100.00",
                "Vendedor.C007_Primeiro_Nome": "LUCAS",
                "Status": "Perdido",
            },
        ]
        fake_api.get_dados.return_value = pd.DataFrame(fake_data)

        criados, atualizados = sincronizar_orcamentos_hardness(
            "01/09/2026", "30/09/2026", empresa_nome="AMM EPIS", api=fake_api
        )
        self.assertEqual(criados, 2)
        self.assertEqual(atualizados, 1)

        # 99901 foi atualizado de PENDENTE para CANCELADO
        orc_99901 = Orcamento.objects.get(empresa="AMM EPIS", numero_orcamento="99901")
        self.assertEqual(orc_99901.status, "CANCELADO")

        # 99902 foi perdido por motivo comercial legítimo (Preço de Concorrente) -> PERDIDO
        orc_99902 = Orcamento.objects.get(empresa="AMM EPIS", numero_orcamento="99902")
        self.assertEqual(orc_99902.status, "PERDIDO")

        # 99903 teve motivo de teste (D047 = 44) -> CANCELADO
        orc_99903 = Orcamento.objects.get(empresa="AMM EPIS", numero_orcamento="99903")
        self.assertEqual(orc_99903.status, "CANCELADO")

    def test_ranking_produtos_preco_e_grafico_causas(self):
        """
        Verifica a geração do gráfico de pizza de causas de perdas comerciais
        e o ranking de produtos não vendidos pelo motivo preço.
        """
        orc_perdido = Orcamento.objects.create(
            empresa="AMM EPIS",
            numero_orcamento="88801",
            data_emissao=date(2026, 9, 20),
            cliente_nome="CLIENTE X",
            vendedor_nome="VENDEDOR 1",
            valor_total=Decimal("800.00"),
            status="PERDIDO",
            flag_perdido="S",
            motivo_perda="Perdeu por preço",
            dados_brutos={"T003_D047_Id": "41"},  # PREÇO DE CONCORRENTE
        )
        ItemOrcamento.objects.create(
            id_item_erp="1-1001",
            empresa="AMM EPIS",
            numero_orcamento="88801",
            orcamento=orc_perdido,
            codigo_produto="PROD-01",
            descricao_produto="LUVA NITRILICA",
            marca="MARCA A",
            quantidade=Decimal("100.00"),
            valor_unitario=Decimal("8.00"),
            valor_total=Decimal("800.00"),
            data_emissao=date(2026, 9, 20),
            cliente_nome="CLIENTE X",
            vendedor_nome="VENDEDOR 1",
        )

        kpis, _, _, grafico_causas, _, _, ranking_preco = gerar_dashboard_orcamentos(
            mes_selecionado=9,
            ano_selecionado=2026,
            gerar_graficos=True,
        )

        self.assertIn("plotly", grafico_causas.lower())
        self.assertIn("Concorrente", grafico_causas)
        self.assertEqual(len(ranking_preco), 1)
        self.assertEqual(ranking_preco[0]["codigo_produto"], "PROD-01")
        self.assertEqual(ranking_preco[0]["quantidade"], 100.0)
        self.assertEqual(ranking_preco[0]["valor_total"], 800.0)
        self.assertEqual(kpis["ranking_preco_total_produtos"], 1)
        self.assertEqual(kpis["ranking_preco_total_valor"], "R$ 800.00")

