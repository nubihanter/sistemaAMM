import base64
import json
import math
import uuid
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd
import plotly.express as px
from plotly.utils import PlotlyJSONEncoder
from django.utils import timezone

from .models import ProdutoEPI, ItemVenda


def _clean_bdata(obj):
    if isinstance(obj, dict):
        if "bdata" in obj and "dtype" in obj:
            raw = base64.b64decode(obj["bdata"])
            return np.frombuffer(raw, dtype=obj["dtype"]).tolist()
        return {k: _clean_bdata(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_clean_bdata(x) for x in obj]
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def fig_to_html(fig):
    """Renderiza figura Plotly convertendo arrays binários bdata (Plotly 6+) para listas JSON puras."""
    fig_dict = _clean_bdata(fig.to_plotly_json())
    div_id = f"plotly-{uuid.uuid4().hex[:12]}"
    data_json = json.dumps(fig_dict.get("data", []), cls=PlotlyJSONEncoder)
    layout_json = json.dumps(fig_dict.get("layout", {}), cls=PlotlyJSONEncoder)
    height = fig_dict.get("layout", {}).get("height", 360)
    return (
        f'<div id="{div_id}" class="plotly-graph-div" style="height:{height}px; width:100%;"></div>'
        f'<script>if(window.Plotly){{Plotly.newPlot("{div_id}", {data_json}, {layout_json}, {{responsive: true}});}}</script>'
    )


STATUS_CONFIG = {
    "CRÍTICO / RUPTURA": {"cor": "#dc2626", "badge": "bg-danger", "ordem": 1},
    "COMPRAR": {"cor": "#ea580c", "badge": "bg-warning text-dark", "ordem": 2},
    "ESPORÁDICO": {"cor": "#7c3aed", "badge": "bg-purple text-white", "ordem": 3},
    "ACIMA DO NECESSÁRIO": {"cor": "#0284c7", "badge": "bg-info text-dark", "ordem": 4},
    "PARADO (SEM GIRO)": {"cor": "#475569", "badge": "bg-secondary", "ordem": 5},
    "SAUDÁVEL": {"cor": "#16a34a", "badge": "bg-success", "ordem": 6},
    "SEM ESTOQUE / INATIVO": {"cor": "#94a3b8", "badge": "bg-light text-muted border", "ordem": 7},
}


def gerar_analise_estoque_e_compras(
    dias_analise: int = 90,
    cobertura_meses: float = 2.0,
    teto_meses: float = 4.0,
    cliente_selecionado: str = "",
    marca_selecionada: str = "TODAS",
    status_filtro: str = "TODOS",
    apenas_criticos: bool = False,
    gerar_graficos: bool = True,
    incluir_aba_cliente: bool = True,
):
    hoje = timezone.now().date()
    data_corte = hoje - timedelta(days=dias_analise)
    meses_periodo = max(dias_analise / 30.0, 0.5)

    # 1. Carrega catálogo completo de Produtos (Estoque)
    produtos_qs = ProdutoEPI.objects.select_related("categoria", "ca").values(
        "id",
        "sku",
        "nome",
        "marca",
        "categoria__nome",
        "ca__numero_ca",
        "ca__data_validade",
        "tamanho_variacao",
        "unidade_medida",
        "preco_custo",
        "preco_venda",
        "estoque_atual",
        "estoque_fisico",
        "qtd_ordem_compra",
        "estoque_minimo",
        "estoque_maximo",
        "item_critico",
        "nao_comprar_erp",
        "data_ultima_entrada",
        "data_ultima_saida",
        "ativo",
    )
    df_cat = pd.DataFrame(list(produtos_qs))

    # 2. Carrega histórico de Itens Vendidos no período selecionado
    itens_qs = ItemVenda.objects.filter(data_emissao__gte=data_corte).values(
        "codigo_produto",
        "descricao_produto",
        "marca",
        "unidade",
        "numero_nota",
        "data_emissao",
        "cliente_nome",
        "vendedor_nome",
        "quantidade",
        "valor_unitario",
        "valor_custo_unitario",
        "valor_total",
    )
    df_vendas = pd.DataFrame(list(itens_qs))

    # Última venda global por produto (para saber há quantos dias um item parado não vende)
    from django.db.models import Max
    ult_qs = ItemVenda.objects.values("codigo_produto").annotate(ultima_venda=Max("data_emissao"))
    ultimas_vendas_globais = {r["codigo_produto"]: r["ultima_venda"] for r in ult_qs}

    # 3. Agregação de vendas por SKU no período + Série Mensal para Regressão Linear de Demanda
    num_meses_janela = max(2, int(round(dias_analise / 30.0)))
    tamanho_fatia_dias = max(1.0, float(dias_analise) / float(num_meses_janela))
    fator_mensalizacao = 30.0 / tamanho_fatia_dias
    t_vals = np.arange(1, num_meses_janela + 1, dtype=float)
    t_medio = float(t_vals.mean())
    t_var_sum = float(((t_vals - t_medio) ** 2).sum())

    if not df_vendas.empty:
        df_vendas["quantidade"] = pd.to_numeric(df_vendas["quantidade"], errors="coerce").fillna(0.0)
        df_vendas["valor_total"] = pd.to_numeric(df_vendas["valor_total"], errors="coerce").fillna(0.0)
        df_vendas["valor_unitario"] = pd.to_numeric(df_vendas["valor_unitario"], errors="coerce").fillna(0.0)
        df_vendas["data_emissao_dt"] = pd.to_datetime(df_vendas["data_emissao"])
        df_vendas["ano_mes"] = df_vendas["data_emissao_dt"].dt.to_period("M")

        # Fatia temporal (1 = mês mais antigo ... N = mês mais recente de 30 dias)
        dias_atras = (pd.to_datetime(hoje) - df_vendas["data_emissao_dt"]).dt.days.clip(lower=0)
        idx_reverso = (dias_atras // tamanho_fatia_dias).clip(lower=0, upper=num_meses_janela - 1).astype(int)
        df_vendas["fatia_mes"] = num_meses_janela - idx_reverso

        # Matriz de vendas mensais por SKU (colunas 1..N)
        piv_mensal = (
            df_vendas.pivot_table(
                index="codigo_produto",
                columns="fatia_mes",
                values="quantidade",
                aggfunc="sum",
                fill_value=0.0,
            )
            * fator_mensalizacao
        )
        for m_i in range(1, num_meses_janela + 1):
            if m_i not in piv_mensal.columns:
                piv_mensal[m_i] = 0.0
        piv_mensal = piv_mensal[list(range(1, num_meses_janela + 1))]

        # Inclinação (slope b) da regressão linear y(t) = a + b*t (em unidades/mês)
        y_matrix = piv_mensal.values
        pesos_t = (t_vals - t_medio) / t_var_sum
        slopes = y_matrix.dot(pesos_t)

        # String resumida do histórico mês a mês (M1..MN) para tooltip
        hist_strs = []
        for row_vals in y_matrix:
            partes = [f"M{i+1}: {val:.0f}" for i, val in enumerate(row_vals)]
            hist_strs.append(" | ".join(partes))

        df_tendencia = pd.DataFrame(
            {
                "codigo_produto": piv_mensal.index,
                "Tendencia_Mes": np.round(slopes, 2),
                "Historico_Mensal_Str": hist_strs,
            }
        )

        df_vendas_agg = (
            df_vendas.groupby("codigo_produto")
            .agg(
                Qtd_Vendida=("quantidade", "sum"),
                Faturamento_Periodo=("valor_total", "sum"),
                Num_Pedidos=("numero_nota", "nunique"),
                Num_Clientes=("cliente_nome", "nunique"),
                Meses_Com_Venda=("ano_mes", "nunique"),
                Ultima_Venda_Periodo=("data_emissao", "max"),
                Descricao_Venda=("descricao_produto", "first"),
                Marca_Venda=("marca", "first"),
                Unidade_Venda=("unidade", "first"),
            )
            .reset_index()
        )
        df_vendas_agg = pd.merge(df_vendas_agg, df_tendencia, on="codigo_produto", how="left")
    else:
        df_vendas_agg = pd.DataFrame(
            columns=[
                "codigo_produto",
                "Qtd_Vendida",
                "Faturamento_Periodo",
                "Num_Pedidos",
                "Num_Clientes",
                "Meses_Com_Venda",
                "Ultima_Venda_Periodo",
                "Descricao_Venda",
                "Marca_Venda",
                "Unidade_Venda",
                "Tendencia_Mes",
                "Historico_Mensal_Str",
            ]
        )

    # 4. Outer Join entre Catálogo de Estoque e Vendas (GARANTE QUE ITENS PARADOS NUNCA SUMAM!)
    if not df_cat.empty:
        df_cat = df_cat.rename(columns={"sku": "codigo_produto"})
        df_base = pd.merge(df_cat, df_vendas_agg, on="codigo_produto", how="outer")
    else:
        df_base = df_vendas_agg.copy()
        for col, default in [
            ("id", None),
            ("nome", ""),
            ("marca", "N/A"),
            ("categoria__nome", "GERAL"),
            ("ca__numero_ca", None),
            ("ca__data_validade", None),
            ("tamanho_variacao", ""),
            ("unidade_medida", "UN"),
            ("preco_custo", 0.0),
            ("preco_venda", 0.0),
            ("estoque_atual", 0),
            ("estoque_fisico", 0),
            ("qtd_ordem_compra", 0),
            ("estoque_minimo", 0),
            ("estoque_maximo", 0),
            ("item_critico", False),
            ("nao_comprar_erp", False),
            ("data_ultima_entrada", None),
            ("data_ultima_saida", None),
            ("ativo", True),
        ]:
            df_base[col] = default

    if df_base.empty:
        return {
            "kpis": {},
            "graficos": {},
            "tabela_compras": [],
            "tabela_ranking": [],
            "tabela_parados": [],
            "tabela_cliente_produtos": [],
            "resumo_cliente": {},
            "tabela_fornecedores_comprados": [],
            "tabela_fornecedores_vendas": [],
            "fornecedores_em_ascensao": [],
            "fornecedores_em_queda": [],
            "resumo_fornecedores": {},
            "marcas_disponiveis": [],
            "clientes_disponiveis": [],
        }

    # Preenche metadados para SKUs que só apareceram nas notas fiscais e não estavam na grade de estoque
    df_base["nome"] = df_base["nome"].fillna(df_base["Descricao_Venda"]).fillna(df_base["codigo_produto"])
    df_base["marca"] = df_base["marca"].fillna(df_base["Marca_Venda"]).fillna("N/A")
    df_base["marca"] = df_base["marca"].replace({"": "N/A", "NAN": "N/A", "NONE": "N/A"})
    df_base["unidade_medida"] = df_base["unidade_medida"].fillna(df_base["Unidade_Venda"]).fillna("UN")
    df_base["categoria__nome"] = df_base["categoria__nome"].fillna("GERAL")
    df_base["ca__numero_ca"] = df_base["ca__numero_ca"].fillna("-")
    if "Tendencia_Mes" not in df_base.columns:
        df_base["Tendencia_Mes"] = 0.0
    if "Historico_Mensal_Str" not in df_base.columns:
        df_base["Historico_Mensal_Str"] = "Sem vendas no período"
    df_base["Historico_Mensal_Str"] = df_base["Historico_Mensal_Str"].fillna("Sem vendas no período")

    for num_col in ["Qtd_Vendida", "Faturamento_Periodo", "preco_custo", "preco_venda", "Tendencia_Mes"]:
        df_base[num_col] = pd.to_numeric(df_base[num_col], errors="coerce").fillna(0.0).astype(float)

    for int_col in ["estoque_atual", "estoque_fisico", "qtd_ordem_compra", "estoque_minimo", "estoque_maximo", "Num_Pedidos", "Num_Clientes", "Meses_Com_Venda"]:
        df_base[int_col] = pd.to_numeric(df_base[int_col], errors="coerce").fillna(0).astype(int)

    df_base["item_critico"] = df_base["item_critico"].fillna(False).astype(bool)
    df_base["nao_comprar_erp"] = df_base["nao_comprar_erp"].fillna(False).astype(bool)
    df_base["ativo"] = df_base["ativo"].fillna(True).astype(bool)

    # Remove produtos totalmente zerados em tudo e sem histórico (lixo de pré-cadastro inativo)
    df_base = df_base[
        (df_base["Qtd_Vendida"] > 0)
        | (df_base["estoque_atual"] != 0)
        | (df_base["estoque_fisico"] != 0)
        | (df_base["item_critico"])
        | (df_base["estoque_minimo"] > 0)
    ].copy()

    # Preço médio de venda e custo estimado (vetorizados)
    df_base["Preco_Medio_Venda"] = np.where(
        df_base["Qtd_Vendida"] > 0,
        df_base["Faturamento_Periodo"] / df_base["Qtd_Vendida"].replace(0.0, np.nan),
        df_base["preco_venda"],
    )
    df_base["Custo_Ref"] = np.where(
        df_base["preco_custo"] > 0,
        df_base["preco_custo"],
        df_base["Preco_Medio_Venda"] * 0.65,
    )

    # Média mensal de giro
    df_base["Vendas_Media_Mes"] = df_base["Qtd_Vendida"] / meses_periodo

    # Curva ABC de Faturamento (para ajudar a encontrar itens críticos que não podem faltar)
    df_base = df_base.sort_values("Faturamento_Periodo", ascending=False).reset_index(drop=True)
    fat_total_geral = df_base["Faturamento_Periodo"].sum()
    if fat_total_geral > 0:
        df_base["Pct_Faturamento"] = (df_base["Faturamento_Periodo"] / fat_total_geral) * 100.0
        df_base["Pct_Acumulado"] = df_base["Pct_Faturamento"].cumsum()
    else:
        df_base["Pct_Faturamento"] = 0.0
        df_base["Pct_Acumulado"] = 100.0

    is_first_row = df_base.index == 0
    df_base["Curva_ABC"] = np.select(
        [
            df_base["Faturamento_Periodo"] <= 0,
            (df_base["Pct_Acumulado"] <= 80.0) | is_first_row,
            df_base["Pct_Acumulado"] <= 95.0,
        ],
        ["S/V", "A", "B"],
        default="C",
    )

    # Classificação Vetorizada de Padrão de Demanda (Recorrente vs Esporádico vs Sem Giro)
    if dias_analise >= 60:
        cond_esporadico = (df_base["Num_Pedidos"] <= 1) | (
            (df_base["Num_Pedidos"] == 2) & (df_base["Meses_Com_Venda"] == 1) & (df_base["Num_Clientes"] == 1)
        )
    else:
        cond_esporadico = (df_base["Num_Pedidos"] == 1) & (df_base["Num_Clientes"] == 1)

    df_base["Padrao_Demanda"] = np.select(
        [
            df_base["Qtd_Vendida"] <= 0,
            df_base["item_critico"],
            cond_esporadico,
        ],
        ["Sem Giro", "Recorrente", "Esporádico"],
        default="Recorrente",
    )

    # Candidato sugerido a Crítico (Curva A ou alta frequência >= 5 pedidos no período)
    df_base["Sugestao_Critico"] = (
        (df_base["item_critico"])
        | ((df_base["Curva_ABC"] == "A") & (df_base["Padrao_Demanda"] == "Recorrente"))
        | (df_base["Num_Pedidos"] >= 6)
    )

    def projetar_demanda_linear(media_mes: float, tendencia_mes: float, meses_frente: float) -> float:
        """
        Projeta a demanda acumulada para os próximos `meses_frente` meses usando regressão linear:
        y(t) = a + b*t, onde t = 1..N são os meses históricos e t = N+1..N+M são os meses futuros.
        """
        if media_mes <= 0 and tendencia_mes <= 0:
            return 0.0
        a = media_mes - (tendencia_mes * t_medio)
        total_projetado = 0.0
        meses_int = int(math.floor(meses_frente))
        frac = meses_frente - meses_int
        for k in range(1, meses_int + 1):
            t_futuro = num_meses_janela + k
            demanda_mes_k = max(0.0, a + tendencia_mes * t_futuro)
            total_projetado += demanda_mes_k
        if frac > 0:
            t_futuro = num_meses_janela + meses_int + 1
            demanda_mes_frac = max(0.0, a + tendencia_mes * t_futuro)
            total_projetado += frac * demanda_mes_frac
        return total_projetado

    # Cálculo de Projeção Linear de Cobertura, Teto de Excesso, Sugestão de Compra e Status (via tuplas rápidas)
    def calcular_planejamento_tupla(row):
        est_atual = int(row["estoque_atual"])
        est_Disp = max(0, est_atual)
        qtd_oc = max(0, int(row["qtd_ordem_compra"]))
        est_min = int(row["estoque_minimo"])
        est_max_manual = int(row["estoque_maximo"])
        media_mes = float(row["Vendas_Media_Mes"])
        tendencia_mes = float(row["Tendencia_Mes"])
        padrao = row["Padrao_Demanda"]

        demanda_prox_mes = round(projetar_demanda_linear(media_mes, tendencia_mes, 1.0), 1)
        projecao_cobertura = int(math.ceil(projetar_demanda_linear(media_mes, tendencia_mes, cobertura_meses)))
        meta_estoque = max(projecao_cobertura, est_min)

        if est_max_manual > 0:
            teto_estoque = est_max_manual
        else:
            projecao_teto = int(math.ceil(projetar_demanda_linear(media_mes, tendencia_mes, teto_meses)))
            teto_estoque = max(projecao_teto, int(math.ceil(media_mes * teto_meses)), est_min * 2, 10)

        taxa_giro_ref = max(demanda_prox_mes, media_mes * 0.25) if media_mes > 0 else 0.0
        if taxa_giro_ref > 0:
            cobertura_atual_meses = round(max(0.0, float(est_atual)) / taxa_giro_ref, 1)
        else:
            cobertura_atual_meses = 99.0 if est_atual > 0 else 0.0

        ult_venda = ultimas_vendas_globais.get(row["codigo_produto"]) or row["data_ultima_saida"]
        dias_sem_vender = (hoje - ult_venda).days if ult_venda else None

        qtd_sugerida_compra = 0
        qtd_excesso = 0

        if row["Qtd_Vendida"] <= 0:
            if (row["item_critico"] or est_min > 0) and (est_Disp + qtd_oc) < est_min:
                status = "CRÍTICO / RUPTURA"
                qtd_sugerida_compra = max(0, est_min - est_Disp - qtd_oc)
            elif est_atual > 0:
                status = "PARADO (SEM GIRO)"
                qtd_excesso = est_atual
            else:
                status = "SEM ESTOQUE / INATIVO"
        elif padrao == "Esporádico" and not row["item_critico"]:
            if est_min > 0 and (est_Disp + qtd_oc) < est_min:
                status = "COMPRAR"
                qtd_sugerida_compra = max(0, est_min - est_Disp - qtd_oc)
            elif est_atual > teto_estoque and est_atual >= 10:
                status = "ACIMA DO NECESSÁRIO"
                qtd_excesso = est_atual - teto_estoque
            else:
                status = "ESPORÁDICO"
                qtd_sugerida_compra = 0
        else:
            saldo_projetado = est_atual + qtd_oc
            if (
                est_atual <= 0
                or (est_min > 0 and saldo_projetado <= est_min)
                or (row["item_critico"] and saldo_projetado < meta_estoque)
                or (cobertura_atual_meses < 0.5 and saldo_projetado < meta_estoque)
            ):
                status = "CRÍTICO / RUPTURA"
                qtd_sugerida_compra = max(0, meta_estoque - max(0, est_atual) - qtd_oc)
            elif saldo_projetado < meta_estoque:
                status = "COMPRAR"
                qtd_sugerida_compra = max(0, meta_estoque - max(0, est_atual) - qtd_oc)
            elif est_atual > teto_estoque:
                status = "ACIMA DO NECESSÁRIO"
                qtd_excesso = est_atual - teto_estoque
            else:
                status = "SAUDÁVEL"

        custo_unit = float(row["Custo_Ref"])
        valor_compra_estimado = round(qtd_sugerida_compra * custo_unit, 2)
        valor_estoque_custo = round(max(0, est_atual) * custo_unit, 2)
        valor_excesso_custo = round(max(0, qtd_excesso) * custo_unit, 2)

        ca_num = str(row.get("ca__numero_ca") or "-").strip()
        ca_dt_raw = row.get("ca__data_validade")
        tem_validade_ca = False
        dias_para_vencer_ca = 0
        validade_ca_str = "-"
        status_ca = "-"

        if ca_dt_raw is not None and not pd.isna(ca_dt_raw):
            if isinstance(ca_dt_raw, datetime):
                ca_dt = ca_dt_raw.date()
            elif isinstance(ca_dt_raw, date):
                ca_dt = ca_dt_raw
            else:
                try:
                    ca_dt = pd.to_datetime(ca_dt_raw).date()
                except Exception:
                    ca_dt = None

            if ca_dt:
                tem_validade_ca = True
                dias_para_vencer_ca = int((ca_dt - hoje).days)
                validade_ca_str = ca_dt.strftime("%d/%m/%Y")
                if dias_para_vencer_ca < 0:
                    status_ca = "VENCIDO"
                elif dias_para_vencer_ca <= 60:
                    status_ca = "A VENCER"
                else:
                    status_ca = "VÁLIDO"
        elif ca_num not in ("-", "", "None", "nan"):
            validade_ca_str = "Pendente"
            status_ca = "PENDENTE"

        return (
            demanda_prox_mes,
            projecao_cobertura,
            meta_estoque,
            teto_estoque,
            cobertura_atual_meses,
            status,
            STATUS_CONFIG[status]["badge"],
            STATUS_CONFIG[status]["ordem"],
            qtd_sugerida_compra,
            valor_compra_estimado,
            qtd_excesso,
            valor_estoque_custo,
            valor_excesso_custo,
            ult_venda.strftime("%d/%m/%Y") if ult_venda else "Sem registro",
            dias_sem_vender if dias_sem_vender is not None else 999,
            tem_validade_ca,
            dias_para_vencer_ca,
            validade_ca_str,
            status_ca,
        )

    planejamento_cols = pd.DataFrame.from_records(
        [calcular_planejamento_tupla(r) for r in df_base.to_dict("records")],
        index=df_base.index,
        columns=[
            "Demanda_Prox_Mes",
            "Projecao_Cobertura",
            "Meta_Estoque",
            "Teto_Estoque",
            "Cobertura_Meses",
            "Status",
            "Badge_Class",
            "Ordem_Status",
            "Qtd_Sugerida_Compra",
            "Valor_Compra_Estimado",
            "Qtd_Excesso",
            "Valor_Estoque_Custo",
            "Valor_Excesso_Custo",
            "Ultima_Venda_Str",
            "Dias_Sem_Vender",
            "Tem_Validade_CA",
            "Dias_Para_Vencer_CA",
            "Validade_CA_Str",
            "Status_CA",
        ],
    )
    df_base = pd.concat([df_base, planejamento_cols], axis=1)

    # 5. KPIs Globais (antes dos filtros de tabela para manter visão executiva no topo)
    df_criticos = df_base[df_base["Status"] == "CRÍTICO / RUPTURA"]
    df_comprar = df_base[df_base["Status"].isin(["CRÍTICO / RUPTURA", "COMPRAR"])]
    df_excesso = df_base[df_base["Status"] == "ACIMA DO NECESSÁRIO"]
    df_parados = df_base[df_base["Status"] == "PARADO (SEM GIRO)"]
    df_esporadicos = df_base[df_base["Padrao_Demanda"] == "Esporádico"]

    unidades_total_estoque = int(df_base[df_base["estoque_atual"] > 0]["estoque_atual"].sum())
    skus_com_estoque = int((df_base["estoque_atual"] > 0).sum())
    valor_total_estoque_num = float(df_base["Valor_Estoque_Custo"].sum())
    valor_total_estoque_venda_num = float((df_base["estoque_atual"].clip(lower=0) * df_base["preco_venda"]).sum())

    kpis = {
        "total_skus": len(df_base),
        "qtd_criticos": len(df_criticos),
        "qtd_comprar": len(df_comprar),
        "valor_total_compra": f"R$ {df_comprar['Valor_Compra_Estimado'].sum():,.2f}",
        "qtd_unidades_comprar": int(df_comprar["Qtd_Sugerida_Compra"].sum()),
        "qtd_excesso": len(df_excesso),
        "unidades_excesso": int(df_excesso["Qtd_Excesso"].sum()),
        "valor_excesso": f"R$ {df_excesso['Valor_Excesso_Custo'].sum():,.2f}",
        "qtd_parados": len(df_parados),
        "unidades_paradas": int(df_parados["estoque_atual"].sum()),
        "valor_parado": f"R$ {df_parados['Valor_Estoque_Custo'].sum():,.2f}",
        "qtd_esporadicos": len(df_esporadicos),
        "faturamento_periodo": f"R$ {df_base['Faturamento_Periodo'].sum():,.2f}",
        "qtd_total_vendida": int(df_base["Qtd_Vendida"].sum()),
        "valor_total_estoque": f"R$ {valor_total_estoque_num:,.2f}",
        "valor_total_estoque_venda": f"R$ {valor_total_estoque_venda_num:,.2f}",
        "unidades_total_estoque": unidades_total_estoque,
        "skus_com_estoque": skus_com_estoque,
    }

    # 6. Listas para Seletores
    marcas_disponiveis = sorted(
        [m for m in df_base["marca"].dropna().unique() if str(m).strip() not in ("", "N/A")]
    )

    # 7. Aplicação de Filtros na Tabela de Planejamento de Compras
    df_filtrado = df_base.copy()
    if marca_selecionada and marca_selecionada != "TODAS":
        df_filtrado = df_filtrado[df_filtrado["marca"] == marca_selecionada]
    if status_filtro and status_filtro != "TODOS":
        df_filtrado = df_filtrado[df_filtrado["Status"] == status_filtro]
    if apenas_criticos:
        df_filtrado = df_filtrado[df_filtrado["item_critico"] | df_filtrado["Sugestao_Critico"]]

    # Ordena por criticidade de compra e volume
    df_filtrado = df_filtrado.sort_values(
        by=["Ordem_Status", "item_critico", "Qtd_Sugerida_Compra", "Qtd_Vendida"],
        ascending=[True, False, False, False],
    )

    # 8. Gráficos Plotly (usando fig_to_html sem bdata binário)
    graficos = {}
    df_com_vendas = df_base[df_base["Qtd_Vendida"] > 0].copy()

    if gerar_graficos:
        # 8.1 Gráfico de Saúde do Estoque (Rosca)
        df_status_chart = (
            df_base[df_base["Status"] != "SEM ESTOQUE / INATIVO"]["Status"]
            .value_counts()
            .reset_index()
        )
        df_status_chart.columns = ["Status", "Quantidade"]
        if not df_status_chart.empty:
            mapa_cores = {k: v["cor"] for k, v in STATUS_CONFIG.items()}
            fig_saude = px.pie(
                df_status_chart,
                names="Status",
                values="Quantidade",
                hole=0.45,
                color="Status",
                color_discrete_map=mapa_cores,
                title="📊 Diagnóstico Geral da Saúde do Estoque (SKUs)",
            )
            fig_saude.update_traces(textposition="inside", textinfo="percent+value")
            fig_saude.update_layout(height=360, margin=dict(t=45, b=20, l=20, r=20))
            graficos["saude_estoque"] = fig_to_html(fig_saude)

        # 8.2 Gráfico Top 12 Urgências de Compra (Qtd Sugerida)
        df_top_compra = (
            df_filtrado[df_filtrado["Qtd_Sugerida_Compra"] > 0]
            .sort_values(["Ordem_Status", "Qtd_Sugerida_Compra"], ascending=[True, False])
            .head(12)
            .copy()
        )
        if not df_top_compra.empty:
            df_top_compra["Label"] = (
                df_top_compra["codigo_produto"].astype(str)
                + " - "
                + df_top_compra["nome"].astype(str).str.slice(0, 28)
            )
            fig_compra = px.bar(
                df_top_compra,
                x="Qtd_Sugerida_Compra",
                y="Label",
                orientation="h",
                color="Status",
                color_discrete_map={k: v["cor"] for k, v in STATUS_CONFIG.items()},
                text="Qtd_Sugerida_Compra",
                title=f"🛒 Top 12 Necessidades de Compra (Cobertura {cobertura_meses:g}M)",
                labels={"Qtd_Sugerida_Compra": "Sugestão de Compra (un)", "Label": "Produto"},
            )
            fig_compra.update_layout(
                height=360,
                margin=dict(t=45, b=20, l=20, r=20),
                yaxis=dict(categoryorder="total ascending"),
                showlegend=True,
            )
            graficos["top_compras"] = fig_to_html(fig_compra)

        # 8.3 Gráficos da Aba Ranking: Top 15 por Faturamento (R$) e Top 15 por Quantidade
        if not df_com_vendas.empty:
            top_fat = df_com_vendas.sort_values("Faturamento_Periodo", ascending=False).head(15).copy()
            top_fat["Label"] = top_fat["codigo_produto"].astype(str) + " - " + top_fat["nome"].astype(str).str.slice(0, 30)
            fig_rank_fat = px.bar(
                top_fat,
                x="Faturamento_Periodo",
                y="Label",
                orientation="h",
                color="Curva_ABC",
                color_discrete_map={"A": "#15803d", "B": "#0284c7", "C": "#64748b"},
                text_auto=".2s",
                title=f"💰 Top 15 Produtos Mais Vendidos por Faturamento ({dias_analise}d)",
                labels={"Faturamento_Periodo": "Faturamento (R$)", "Label": "Produto"},
            )
            fig_rank_fat.update_layout(
                height=420,
                margin=dict(t=45, b=20, l=20, r=20),
                yaxis=dict(categoryorder="total ascending"),
            )
            graficos["ranking_faturamento"] = fig_to_html(fig_rank_fat)

            top_qtd = df_com_vendas.sort_values("Qtd_Vendida", ascending=False).head(15).copy()
            top_qtd["Label"] = top_qtd["codigo_produto"].astype(str) + " - " + top_qtd["nome"].astype(str).str.slice(0, 30)
            fig_rank_qtd = px.bar(
                top_qtd,
                x="Qtd_Vendida",
                y="Label",
                orientation="h",
                color="Status",
                color_discrete_map={k: v["cor"] for k, v in STATUS_CONFIG.items()},
                text="Qtd_Vendida",
                title=f"📦 Top 15 Produtos Mais Vendidos por Quantidade ({dias_analise}d)",
                labels={"Qtd_Vendida": "Quantidade Vendida", "Label": "Produto"},
            )
            fig_rank_qtd.update_layout(
                height=420,
                margin=dict(t=45, b=20, l=20, r=20),
                yaxis=dict(categoryorder="total ascending"),
            )
            graficos["ranking_quantidade"] = fig_to_html(fig_rank_qtd)

    # 9. Tabelas Estruturadas
    # 9.1 Tabela de Compras e Alertas
    tabela_compras = df_filtrado.to_dict(orient="records")

    # 9.2 Tabela de Ranking (Produtos que mais vendem por Quantidade e Faturamento)
    df_ranking = df_com_vendas.sort_values("Qtd_Vendida", ascending=False).copy()
    df_ranking["Posicao_Qtd"] = range(1, len(df_ranking) + 1)
    df_ranking = df_ranking.sort_values("Faturamento_Periodo", ascending=False).copy()
    df_ranking["Posicao_Fat"] = range(1, len(df_ranking) + 1)
    tabela_ranking = df_ranking.to_dict(orient="records")

    # 9.3 Tabela de Produtos Parados & Excesso de Estoque
    df_parados_excesso = df_base[
        df_base["Status"].isin(["PARADO (SEM GIRO)", "ACIMA DO NECESSÁRIO"])
    ].sort_values(
        by=["Status", "Qtd_Excesso", "estoque_atual"],
        ascending=[False, False, False],
    )
    tabela_parados = df_parados_excesso.to_dict(orient="records")

    # 10. Visão: Produtos que cada Cliente compra x Situação do Estoque ao lado
    clientes_disponiveis = []
    tabela_cliente_produtos = []
    resumo_cliente = {}

    if incluir_aba_cliente:
        clientes_raw = (
            ItemVenda.objects.exclude(cliente_nome__in=["", "NAN", "NONE"])
            .order_by()
            .values_list("cliente_nome", flat=True)
            .distinct()
        )
        clientes_map = {}
        for nome_cli in clientes_raw:
            if nome_cli:
                nome_limpo = str(nome_cli).strip()
                chave_upper = nome_limpo.upper()
                if chave_upper and chave_upper not in ("NAN", "NONE") and chave_upper not in clientes_map:
                    clientes_map[chave_upper] = nome_limpo
        clientes_disponiveis = sorted(clientes_map.values())

        if not cliente_selecionado and clientes_disponiveis:
            # Seleciona por padrão o cliente com maior volume no período para já exibir a aba preenchida
            if not df_vendas.empty:
                top_cli = (
                    df_vendas.groupby("cliente_nome")["valor_total"]
                    .sum()
                    .sort_values(ascending=False)
                    .index
                )
                cliente_selecionado = top_cli[0].strip() if len(top_cli) > 0 else clientes_disponiveis[0]
            else:
                cliente_selecionado = clientes_disponiveis[0]

    if incluir_aba_cliente and cliente_selecionado:
        itens_cli_qs = ItemVenda.objects.filter(cliente_nome__iexact=cliente_selecionado.strip()).values(
            "codigo_produto",
            "descricao_produto",
            "marca",
            "unidade",
            "numero_nota",
            "data_emissao",
            "quantidade",
            "valor_unitario",
            "valor_total",
            "vendedor_nome",
        )
        df_cli = pd.DataFrame(list(itens_cli_qs))
        if not df_cli.empty:
            df_cli["quantidade"] = pd.to_numeric(df_cli["quantidade"], errors="coerce").fillna(0.0)
            df_cli["valor_total"] = pd.to_numeric(df_cli["valor_total"], errors="coerce").fillna(0.0)
            df_cli["valor_unitario"] = pd.to_numeric(df_cli["valor_unitario"], errors="coerce").fillna(0.0)
            df_cli = df_cli.sort_values("data_emissao")

            df_cli_agg = (
                df_cli.groupby("codigo_produto")
                .agg(
                    Descricao_Cliente=("descricao_produto", "last"),
                    Qtd_Total_Cliente=("quantidade", "sum"),
                    Fat_Total_Cliente=("valor_total", "sum"),
                    Pedidos_Cliente=("numero_nota", "nunique"),
                    Ultimo_Preco_Cliente=("valor_unitario", "last"),
                    Ultima_Compra_Cliente=("data_emissao", "max"),
                    Vendedor_Cliente=("vendedor_nome", "last"),
                )
                .reset_index()
            )
            df_cli_agg["Qtd_Media_Por_Pedido"] = df_cli_agg["Qtd_Total_Cliente"] / df_cli_agg["Pedidos_Cliente"]
            df_cli_agg["Dias_Ultima_Compra"] = df_cli_agg["Ultima_Compra_Cliente"].apply(
                lambda d: (hoje - d).days if d else 0
            )
            df_cli_agg["Ultima_Compra_Str"] = df_cli_agg["Ultima_Compra_Cliente"].apply(
                lambda d: d.strftime("%d/%m/%Y") if d else "-"
            )

            # Cruza os produtos que o cliente compra com o estado atual do estoque na AMM!
            cols_estoque = [
                "codigo_produto",
                "id",
                "nome",
                "marca",
                "unidade_medida",
                "estoque_atual",
                "estoque_fisico",
                "qtd_ordem_compra",
                "estoque_minimo",
                "item_critico",
                "Vendas_Media_Mes",
                "Meta_Estoque",
                "Cobertura_Meses",
                "Status",
                "Badge_Class",
                "Qtd_Sugerida_Compra",
            ]
            df_cli_merged = pd.merge(
                df_cli_agg,
                df_base[cols_estoque],
                on="codigo_produto",
                how="left",
            )
            df_cli_merged["nome"] = df_cli_merged["nome"].fillna(df_cli_merged["Descricao_Cliente"])
            df_cli_merged["estoque_atual"] = df_cli_merged["estoque_atual"].fillna(0).astype(int)
            df_cli_merged["estoque_minimo"] = df_cli_merged["estoque_minimo"].fillna(0).astype(int)
            df_cli_merged["qtd_ordem_compra"] = df_cli_merged["qtd_ordem_compra"].fillna(0).astype(int)
            df_cli_merged["Qtd_Sugerida_Compra"] = df_cli_merged["Qtd_Sugerida_Compra"].fillna(0).astype(int)
            df_cli_merged["Status"] = df_cli_merged["Status"].fillna("SEM ESTOQUE / INATIVO")
            df_cli_merged["Badge_Class"] = df_cli_merged["Badge_Class"].fillna("bg-secondary")

            # Verifica se o estoque atual na AMM cobre pelo menos 1 pedido médio desse cliente
            df_cli_merged["Atende_Pedido_Medio"] = (
                df_cli_merged["estoque_atual"] >= df_cli_merged["Qtd_Media_Por_Pedido"]
            )

            df_cli_merged = df_cli_merged.sort_values("Fat_Total_Cliente", ascending=False)
            tabela_cliente_produtos = df_cli_merged.to_dict(orient="records")

            itens_em_alerta_cliente = len(
                df_cli_merged[
                    df_cli_merged["Status"].isin(["CRÍTICO / RUPTURA", "COMPRAR"])
                    | (~df_cli_merged["Atende_Pedido_Medio"])
                ]
            )
            resumo_cliente = {
                "cliente_nome": cliente_selecionado,
                "total_skus_comprados": len(df_cli_merged),
                "qtd_total_comprada": int(df_cli_merged["Qtd_Total_Cliente"].sum()),
                "faturamento_total": f"R$ {df_cli_merged['Fat_Total_Cliente'].sum():,.2f}",
                "itens_em_risco_estoque": itens_em_alerta_cliente,
                "vendedor": df_cli_merged["Vendedor_Cliente"].iloc[0] if not df_cli_merged.empty else "-",
            }

    # 11. Análise de Fornecedores & Marcas (Base de Giro)
    tabela_fornecedores_comprados = []
    tabela_fornecedores_vendas = []
    fornecedores_em_ascensao = []
    fornecedores_em_queda = []
    resumo_fornecedores = {
        "total_fornecedores_comprados": 0,
        "valor_total_comprado_periodo": "R$ 0,00",
        "total_marcas_ativas": 0,
        "qtd_em_ascensao": 0,
        "qtd_em_queda": 0,
    }

    # 11.1 Ranking dos Fornecedores Mais Comprados (Base de Giro via Contas a Pagar - Compra de Mercadoria)
    from django.db.models import Q
    from sales.models import ContaPagar

    cp_compras_qs = (
        ContaPagar.objects.filter(cancelada=False, data_emissao__gte=data_corte)
        .exclude(status="CANCELADO")
        .filter(
            Q(centro_custo__iexact="COMPRAS/ESTOQUE")
            | Q(grupo_conta__icontains="COMPRA MERCADORIA")
            | Q(subconta__icontains="COMPRA MERCADORIA")
            | Q(grupo_conta__icontains="MATERIA PRIMA")
            | Q(subconta__icontains="MATERIA PRIMA")
        )
        .values(
            "id_titulo_erp",
            "numero_documento",
            "fornecedor_nome",
            "fornecedor_documento",
            "data_emissao",
            "prazo_dias",
            "valor_total",
        )
    )
    df_cp_forn = pd.DataFrame(list(cp_compras_qs))
    if not df_cp_forn.empty:
        df_cp_forn["valor_total"] = pd.to_numeric(df_cp_forn["valor_total"], errors="coerce").fillna(0.0)
        df_cp_forn["prazo_dias"] = pd.to_numeric(df_cp_forn["prazo_dias"], errors="coerce").fillna(0).clip(lower=0)
        df_cp_forn["fornecedor_nome"] = (
            df_cp_forn["fornecedor_nome"]
            .fillna("FORNECEDOR NÃO IDENTIFICADO")
            .astype(str)
            .str.strip()
            .str.upper()
            .replace({"": "FORNECEDOR NÃO IDENTIFICADO", "NAN": "FORNECEDOR NÃO IDENTIFICADO"})
        )
        df_cp_forn["doc_ref"] = np.where(
            df_cp_forn["numero_documento"].fillna("").astype(str).str.strip() != "",
            df_cp_forn["numero_documento"].astype(str).str.strip(),
            df_cp_forn["id_titulo_erp"].astype(str).str.strip(),
        )
        df_forn_comp = (
            df_cp_forn.groupby("fornecedor_nome", as_index=False)
            .agg(
                Valor_Comprado=("valor_total", "sum"),
                Qtd_Notas=("doc_ref", "nunique"),
                Qtd_Titulos=("id_titulo_erp", "count"),
                Prazo_Medio_Dias=("prazo_dias", "mean"),
                Ultima_Compra=("data_emissao", "max"),
            )
            .sort_values(["Valor_Comprado", "Qtd_Notas"], ascending=[False, False])
            .reset_index(drop=True)
        )
        tot_compras_periodo = float(df_forn_comp["Valor_Comprado"].sum())
        df_forn_comp["Fornecedor"] = df_forn_comp["fornecedor_nome"]
        df_forn_comp["Valor_Total_Comprado"] = df_forn_comp["Valor_Comprado"]
        df_forn_comp["Media_Mensal_Compra"] = df_forn_comp["Valor_Comprado"] / meses_periodo
        df_forn_comp["Media_Mensal_Qtd"] = df_forn_comp["Qtd_Titulos"] / meses_periodo
        df_forn_comp["Pct_Compras"] = (
            (df_forn_comp["Valor_Comprado"] / tot_compras_periodo * 100.0) if tot_compras_periodo > 0 else 0.0
        )
        df_forn_comp["Ultima_Compra_Str"] = df_forn_comp["Ultima_Compra"].apply(
            lambda d: d.strftime("%d/%m/%Y") if d and not pd.isna(d) else "-"
        )
        df_forn_comp["Posicao_Valor"] = range(1, len(df_forn_comp) + 1)
        df_forn_comp["Posicao"] = df_forn_comp["Posicao_Valor"]
        df_forn_comp_qtd = df_forn_comp.sort_values(["Qtd_Notas", "Valor_Comprado"], ascending=[False, False]).copy()
        df_forn_comp_qtd["Posicao_Qtd"] = range(1, len(df_forn_comp_qtd) + 1)
        mapa_pos_qtd = dict(zip(df_forn_comp_qtd["fornecedor_nome"], df_forn_comp_qtd["Posicao_Qtd"]))
        df_forn_comp["Posicao_Qtd"] = df_forn_comp["fornecedor_nome"].map(mapa_pos_qtd)

        tabela_fornecedores_comprados = df_forn_comp.to_dict(orient="records")
        resumo_fornecedores["total_fornecedores_comprados"] = len(df_forn_comp)
        resumo_fornecedores["valor_total_comprado_periodo"] = f"R$ {tot_compras_periodo:,.2f}"
        resumo_fornecedores["valor_total_comprado"] = f"R$ {tot_compras_periodo:,.2f}"

    # 11.2 Ranking dos Fornecedores / Marcas que Mais Vendem, Tendência (Ascensão/Queda) e Consumo Médio Mensal por Marca
    df_marcas_base = df_base[df_base["marca"].astype(str).str.upper() != "N/A"].copy()
    if df_marcas_base.empty:
        df_marcas_base = df_base.copy()

    if not df_marcas_base.empty:
        df_marcas_base["Estoque_Positivo"] = df_marcas_base["estoque_atual"].clip(lower=0)
        df_marcas_base["Custo_Giro_SKU"] = df_marcas_base["Qtd_Vendida"] * df_marcas_base["Custo_Ref"]
        df_marcas_base["Tem_Giro"] = (df_marcas_base["Qtd_Vendida"] > 0).astype(int)

        df_marca_agg = (
            df_marcas_base.groupby("marca", as_index=False)
            .agg(
                Total_SKUs=("codigo_produto", "count"),
                SKUs_Com_Giro=("Tem_Giro", "sum"),
                Qtd_Vendida=("Qtd_Vendida", "sum"),
                Faturamento_Periodo=("Faturamento_Periodo", "sum"),
                Custo_Giro_Periodo=("Custo_Giro_SKU", "sum"),
                Estoque_Atual=("Estoque_Positivo", "sum"),
                Qtd_Ordem_Compra=("qtd_ordem_compra", "sum"),
                Qtd_Sugerida_Compra=("Qtd_Sugerida_Compra", "sum"),
                Valor_Compra_Estimado=("Valor_Compra_Estimado", "sum"),
                Valor_Estoque_Custo=("Valor_Estoque_Custo", "sum"),
            )
        )

        # Pedidos e Clientes distintos por Marca no período + Tendência Mensal por Marca
        if not df_vendas.empty:
            df_v_marca = df_vendas.copy()
            mapa_sku_marca = dict(zip(df_base["codigo_produto"], df_base["marca"]))
            df_v_marca["marca_consolidada"] = (
                df_v_marca["codigo_produto"]
                .map(mapa_sku_marca)
                .fillna(df_v_marca["marca"])
                .fillna("N/A")
                .astype(str)
                .str.strip()
                .str.upper()
                .replace({"": "N/A", "NAN": "N/A", "NONE": "N/A"})
            )
            df_v_ped_cli = (
                df_v_marca.groupby("marca_consolidada", as_index=False)
                .agg(
                    Num_Pedidos=("numero_nota", "nunique"),
                    Num_Clientes=("cliente_nome", "nunique"),
                )
                .rename(columns={"marca_consolidada": "marca"})
            )
            df_marca_agg = pd.merge(df_marca_agg, df_v_ped_cli, on="marca", how="left")

            piv_marca_qtd = (
                df_v_marca.pivot_table(
                    index="marca_consolidada",
                    columns="fatia_mes",
                    values="quantidade",
                    aggfunc="sum",
                    fill_value=0.0,
                )
                * fator_mensalizacao
            )
            piv_marca_fat = (
                df_v_marca.pivot_table(
                    index="marca_consolidada",
                    columns="fatia_mes",
                    values="valor_total",
                    aggfunc="sum",
                    fill_value=0.0,
                )
                * fator_mensalizacao
            )
            for m_i in range(1, num_meses_janela + 1):
                if m_i not in piv_marca_qtd.columns:
                    piv_marca_qtd[m_i] = 0.0
                if m_i not in piv_marca_fat.columns:
                    piv_marca_fat[m_i] = 0.0
            cols_janela = list(range(1, num_meses_janela + 1))
            piv_marca_qtd = piv_marca_qtd[cols_janela]
            piv_marca_fat = piv_marca_fat[cols_janela]

            pesos_t = (t_vals - t_medio) / t_var_sum
            slopes_marca_qtd = piv_marca_qtd.values.dot(pesos_t)
            slopes_marca_fat = piv_marca_fat.values.dot(pesos_t)

            # Comparação da metade recente vs metade inicial da janela para % de evolução
            meio_idx = max(1, num_meses_janela // 2)
            media_ini_qtd = piv_marca_qtd[cols_janela[:meio_idx]].mean(axis=1).values
            media_fim_qtd = piv_marca_qtd[cols_janela[meio_idx:]].mean(axis=1).values
            var_pct_marca = np.where(
                media_ini_qtd > 0,
                ((media_fim_qtd - media_ini_qtd) / media_ini_qtd) * 100.0,
                np.where(media_fim_qtd > 0, 100.0, 0.0),
            )

            hist_marca_strs = []
            for row_vals in piv_marca_qtd.values:
                partes = [f"M{i+1}: {val:.0f}" for i, val in enumerate(row_vals)]
                hist_marca_strs.append(" | ".join(partes))

            df_tend_marca = pd.DataFrame(
                {
                    "marca": piv_marca_qtd.index,
                    "Tendencia_Mes_Qtd": np.round(slopes_marca_qtd, 1),
                    "Tendencia_Mes_Fat": np.round(slopes_marca_fat, 2),
                    "Variacao_Pct": np.round(var_pct_marca, 1),
                    "Historico_Mensal_Str": hist_marca_strs,
                    "Media_Recente_Qtd": np.round(media_fim_qtd, 1),
                    "Media_Inicial_Qtd": np.round(media_ini_qtd, 1),
                }
            )
            df_marca_agg = pd.merge(df_marca_agg, df_tend_marca, on="marca", how="left")
        else:
            df_marca_agg["Num_Pedidos"] = 0
            df_marca_agg["Num_Clientes"] = 0
            df_marca_agg["Tendencia_Mes_Qtd"] = 0.0
            df_marca_agg["Tendencia_Mes_Fat"] = 0.0
            df_marca_agg["Variacao_Pct"] = 0.0
            df_marca_agg["Historico_Mensal_Str"] = "Sem vendas no período"
            df_marca_agg["Media_Recente_Qtd"] = 0.0
            df_marca_agg["Media_Inicial_Qtd"] = 0.0

        for c_int in ["Num_Pedidos", "Num_Clientes"]:
            df_marca_agg[c_int] = pd.to_numeric(df_marca_agg[c_int], errors="coerce").fillna(0).astype(int)
        for c_flt in ["Tendencia_Mes_Qtd", "Tendencia_Mes_Fat", "Variacao_Pct", "Media_Recente_Qtd", "Media_Inicial_Qtd"]:
            df_marca_agg[c_flt] = pd.to_numeric(df_marca_agg[c_flt], errors="coerce").fillna(0.0).astype(float)
        df_marca_agg["Historico_Mensal_Str"] = df_marca_agg["Historico_Mensal_Str"].fillna("Sem vendas no período")

        # Consumo médio mensal por marca (venda em unidades/mês e R$/mês)
        df_marca_agg["Fornecedor"] = df_marca_agg["marca"]
        df_marca_agg["SKUs_Ativos"] = df_marca_agg["Total_SKUs"]
        df_marca_agg["Consumo_Medio_Mensal_Qtd"] = np.round(df_marca_agg["Qtd_Vendida"] / meses_periodo, 1)
        df_marca_agg["Consumo_Medio_Mensal_Fat"] = np.round(df_marca_agg["Faturamento_Periodo"] / meses_periodo, 2)

        tot_qtd_marcas = float(df_marca_agg["Qtd_Vendida"].sum())
        tot_fat_marcas = float(df_marca_agg["Faturamento_Periodo"].sum())
        df_marca_agg["Pct_Qtd"] = (
            (df_marca_agg["Qtd_Vendida"] / tot_qtd_marcas * 100.0) if tot_qtd_marcas > 0 else 0.0
        )
        df_marca_agg["Pct_Faturamento"] = (
            (df_marca_agg["Faturamento_Periodo"] / tot_fat_marcas * 100.0) if tot_fat_marcas > 0 else 0.0
        )
        df_marca_agg["Cobertura_Meses"] = np.where(
            df_marca_agg["Consumo_Medio_Mensal_Qtd"] > 0,
            np.round(df_marca_agg["Estoque_Atual"] / df_marca_agg["Consumo_Medio_Mensal_Qtd"], 1),
            np.where(df_marca_agg["Estoque_Atual"] > 0, 99.0, 0.0),
        )

        # Classificação de Tendência (Em Ascensão / Em Queda / Estável)
        df_marca_agg["Status_Tendencia"] = np.select(
            [
                df_marca_agg["Qtd_Vendida"] <= 0,
                (df_marca_agg["Tendencia_Mes_Qtd"] >= 1.0)
                | ((df_marca_agg["Tendencia_Mes_Qtd"] > 0.1) & (df_marca_agg["Variacao_Pct"] >= 10.0)),
                (df_marca_agg["Tendencia_Mes_Qtd"] <= -1.0)
                | ((df_marca_agg["Tendencia_Mes_Qtd"] < -0.1) & (df_marca_agg["Variacao_Pct"] <= -10.0)),
            ],
            ["SEM GIRO", "EM ASCENSÃO", "EM QUEDA"],
            default="ESTÁVEL",
        )
        df_marca_agg["Badge_Tendencia"] = np.select(
            [
                df_marca_agg["Status_Tendencia"] == "EM ASCENSÃO",
                df_marca_agg["Status_Tendencia"] == "EM QUEDA",
                df_marca_agg["Status_Tendencia"] == "ESTÁVEL",
            ],
            [
                "bg-success-subtle text-success border",
                "bg-danger-subtle text-danger border",
                "bg-secondary-subtle text-secondary border",
            ],
            default="bg-light text-muted border",
        )

        # Posições no Ranking de Vendas por Faturamento e por Quantidade
        df_marca_agg = df_marca_agg.sort_values(["Qtd_Vendida", "Faturamento_Periodo"], ascending=[False, False]).reset_index(drop=True)
        df_marca_agg["Posicao_Qtd"] = range(1, len(df_marca_agg) + 1)
        df_marca_agg = df_marca_agg.sort_values(["Faturamento_Periodo", "Qtd_Vendida"], ascending=[False, False]).reset_index(drop=True)
        df_marca_agg["Posicao_Fat"] = range(1, len(df_marca_agg) + 1)

        tabela_fornecedores_vendas = df_marca_agg.to_dict(orient="records")

        df_asc = (
            df_marca_agg[df_marca_agg["Status_Tendencia"] == "EM ASCENSÃO"]
            .sort_values(["Tendencia_Mes_Qtd", "Variacao_Pct"], ascending=[False, False])
            .copy()
        )
        df_queda = (
            df_marca_agg[df_marca_agg["Status_Tendencia"] == "EM QUEDA"]
            .sort_values(["Tendencia_Mes_Qtd", "Variacao_Pct"], ascending=[True, True])
            .copy()
        )
        fornecedores_em_ascensao = df_asc.to_dict(orient="records")
        fornecedores_em_queda = df_queda.to_dict(orient="records")

        resumo_fornecedores["total_marcas_ativas"] = int((df_marca_agg["Qtd_Vendida"] > 0).sum())
        resumo_fornecedores["qtd_em_ascensao"] = len(df_asc)
        resumo_fornecedores["qtd_ascensao"] = len(df_asc)
        resumo_fornecedores["qtd_em_queda"] = len(df_queda)
        resumo_fornecedores["qtd_queda"] = len(df_queda)

        # Fallback: se Contas a Pagar estiver vazia no período, popula tabela_fornecedores_comprados via Marcas (Custo de Giro + OC)
        if not tabela_fornecedores_comprados:
            df_fb_comp = df_marca_agg.copy()
            df_fb_comp["Valor_Comprado"] = df_fb_comp["Custo_Giro_Periodo"] + df_fb_comp["Valor_Compra_Estimado"]
            df_fb_comp = df_fb_comp[df_fb_comp["Valor_Comprado"] > 0].sort_values("Valor_Comprado", ascending=False).reset_index(drop=True)
            if not df_fb_comp.empty:
                tot_fb = float(df_fb_comp["Valor_Comprado"].sum())
                df_fb_comp["fornecedor_nome"] = df_fb_comp["marca"]
                df_fb_comp["Fornecedor"] = df_fb_comp["marca"]
                df_fb_comp["Valor_Total_Comprado"] = df_fb_comp["Valor_Comprado"]
                df_fb_comp["Media_Mensal_Compra"] = df_fb_comp["Valor_Comprado"] / meses_periodo
                df_fb_comp["Media_Mensal_Qtd"] = df_fb_comp["Total_SKUs"] / meses_periodo
                df_fb_comp["Pct_Compras"] = (df_fb_comp["Valor_Comprado"] / tot_fb * 100.0) if tot_fb > 0 else 0.0
                df_fb_comp["Qtd_Notas"] = df_fb_comp["SKUs_Com_Giro"]
                df_fb_comp["Qtd_Titulos"] = df_fb_comp["Total_SKUs"]
                df_fb_comp["Prazo_Medio_Dias"] = 0.0
                df_fb_comp["Ultima_Compra_Str"] = "-"
                df_fb_comp["Posicao_Valor"] = range(1, len(df_fb_comp) + 1)
                df_fb_comp["Posicao"] = df_fb_comp["Posicao_Valor"]
                df_fb_comp["Posicao_Qtd"] = range(1, len(df_fb_comp) + 1)
                tabela_fornecedores_comprados = df_fb_comp.to_dict(orient="records")
                resumo_fornecedores["total_fornecedores_comprados"] = len(df_fb_comp)
                resumo_fornecedores["valor_total_comprado_periodo"] = f"R$ {tot_fb:,.2f}"
                resumo_fornecedores["valor_total_comprado"] = f"R$ {tot_fb:,.2f}"

    if gerar_graficos:
        # Gráfico 1: Ranking dos Fornecedores Mais Comprados (Base de Giro)
        if tabela_fornecedores_comprados:
            df_top_forn_comp = pd.DataFrame(tabela_fornecedores_comprados).head(15).copy()
            df_top_forn_comp["Fornecedor_Curto"] = df_top_forn_comp["fornecedor_nome"].astype(str).str.slice(0, 32)
            fig_forn_comp = px.bar(
                df_top_forn_comp,
                x="Valor_Comprado",
                y="Fornecedor_Curto",
                orientation="h",
                text_auto=".2s",
                title=f"🏭 Top 15 Fornecedores Mais Comprados em R$ ({dias_analise}d)",
                labels={"Valor_Comprado": "Volume Comprado (R$)", "Fornecedor_Curto": "Fornecedor"},
            )
            fig_forn_comp.update_traces(marker_color="#1e3a8a")
            fig_forn_comp.update_layout(
                height=420,
                margin=dict(t=45, b=20, l=20, r=20),
                yaxis=dict(categoryorder="total ascending"),
            )
            graficos["fornecedores_mais_comprados"] = fig_to_html(fig_forn_comp)

            df_top_forn_qtd = (
                pd.DataFrame(tabela_fornecedores_comprados)
                .sort_values(["Qtd_Notas", "Valor_Comprado"], ascending=[False, False])
                .head(15)
                .copy()
            )
            df_top_forn_qtd["Fornecedor_Curto"] = df_top_forn_qtd["fornecedor_nome"].astype(str).str.slice(0, 32)
            fig_forn_comp_q = px.bar(
                df_top_forn_qtd,
                x="Qtd_Notas",
                y="Fornecedor_Curto",
                orientation="h",
                text="Qtd_Notas",
                title=f"🏭 Top 15 Fornecedores Mais Comprados por Nº de Pedidos/NFs ({dias_analise}d)",
                labels={"Qtd_Notas": "Nº de NFs / Pedidos de Compra", "Fornecedor_Curto": "Fornecedor"},
            )
            fig_forn_comp_q.update_traces(marker_color="#1e3a8a")
            fig_forn_comp_q.update_layout(
                height=420,
                margin=dict(t=45, b=20, l=20, r=20),
                yaxis=dict(categoryorder="total ascending"),
            )
            graficos["fornecedores_mais_comprados_qtd"] = fig_to_html(fig_forn_comp_q)

        # Gráfico 2: Ranking dos Fornecedores / Marcas que Mais Vendem (Base de Giro)
        if tabela_fornecedores_vendas:
            df_fv = pd.DataFrame(tabela_fornecedores_vendas)
            df_fv_vendas = df_fv[df_fv["Qtd_Vendida"] > 0].copy()
            if not df_fv_vendas.empty:
                top_fv_fat = df_fv_vendas.sort_values("Faturamento_Periodo", ascending=False).head(15).copy()
                fig_fv_fat = px.bar(
                    top_fv_fat,
                    x="Faturamento_Periodo",
                    y="marca",
                    orientation="h",
                    text_auto=".2s",
                    title=f"💰 Top 15 Fornecedores / Marcas que Mais Vendem em R$ ({dias_analise}d)",
                    labels={"Faturamento_Periodo": "Faturamento de Venda (R$)", "marca": "Marca / Fornecedor"},
                )
                fig_fv_fat.update_traces(marker_color="#15803d")
                fig_fv_fat.update_layout(
                    height=420,
                    margin=dict(t=45, b=20, l=20, r=20),
                    yaxis=dict(categoryorder="total ascending"),
                )
                graficos["fornecedores_mais_vendem_fat"] = fig_to_html(fig_fv_fat)

                top_fv_qtd = df_fv_vendas.sort_values("Qtd_Vendida", ascending=False).head(15).copy()
                fig_fv_qtd = px.bar(
                    top_fv_qtd,
                    x="Qtd_Vendida",
                    y="marca",
                    orientation="h",
                    text="Qtd_Vendida",
                    title=f"📦 Top 15 Fornecedores / Marcas que Mais Vendem em Quantidade ({dias_analise}d)",
                    labels={"Qtd_Vendida": "Quantidade Vendida (un)", "marca": "Marca / Fornecedor"},
                )
                fig_fv_qtd.update_traces(marker_color="#0284c7")
                fig_fv_qtd.update_layout(
                    height=420,
                    margin=dict(t=45, b=20, l=20, r=20),
                    yaxis=dict(categoryorder="total ascending"),
                )
                graficos["fornecedores_mais_vendem_qtd"] = fig_to_html(fig_fv_qtd)

                # Gráfico 3: Consumo Médio Mensal por Marca (Venda)
                top_cons_mes = df_fv_vendas.sort_values("Consumo_Medio_Mensal_Qtd", ascending=False).head(15).copy()
                fig_cons_marca = px.bar(
                    top_cons_mes,
                    x="Consumo_Medio_Mensal_Qtd",
                    y="marca",
                    orientation="h",
                    text="Consumo_Medio_Mensal_Qtd",
                    title=f"📊 Consumo Médio Mensal por Marca — Venda em un/mês (Base {dias_analise}d)",
                    labels={"Consumo_Medio_Mensal_Qtd": "Consumo Médio Mensal (un/mês)", "marca": "Marca"},
                )
                fig_cons_marca.update_traces(marker_color="#7c3aed")
                fig_cons_marca.update_layout(
                    height=420,
                    margin=dict(t=45, b=20, l=20, r=20),
                    yaxis=dict(categoryorder="total ascending"),
                )
                graficos["consumo_medio_mensal_marca"] = fig_to_html(fig_cons_marca)

                # Gráfico 4: Fornecedores em Ascensão e Caindo
                df_tend_plot = pd.concat(
                    [
                        df_fv_vendas[df_fv_vendas["Tendencia_Mes_Qtd"] > 0]
                        .sort_values("Tendencia_Mes_Qtd", ascending=False)
                        .head(8),
                        df_fv_vendas[df_fv_vendas["Tendencia_Mes_Qtd"] < 0]
                        .sort_values("Tendencia_Mes_Qtd", ascending=True)
                        .head(8),
                    ],
                    ignore_index=True,
                ).drop_duplicates(subset=["marca"])
                if not df_tend_plot.empty:
                    df_tend_plot["Direcao"] = np.where(
                        df_tend_plot["Tendencia_Mes_Qtd"] >= 0,
                        "Em Ascensão (+un/mês)",
                        "Caindo (-un/mês)",
                    )
                    df_tend_plot = df_tend_plot.sort_values("Tendencia_Mes_Qtd", ascending=True)
                    fig_tend = px.bar(
                        df_tend_plot,
                        x="Tendencia_Mes_Qtd",
                        y="marca",
                        orientation="h",
                        color="Direcao",
                        color_discrete_map={
                            "Em Ascensão (+un/mês)": "#16a34a",
                            "Caindo (-un/mês)": "#dc2626",
                        },
                        text="Tendencia_Mes_Qtd",
                        title=f"📈📉 Fornecedores / Marcas em Ascensão vs. Caindo ({dias_analise}d)",
                        labels={"Tendencia_Mes_Qtd": "Tendência Mensal (un/mês)", "marca": "Marca / Fornecedor"},
                    )
                    fig_tend.update_layout(
                        height=420,
                        margin=dict(t=45, b=20, l=20, r=20),
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    )
                    graficos["fornecedores_tendencia"] = fig_to_html(fig_tend)

    return {
        "kpis": kpis,
        "graficos": graficos,
        "tabela_compras": tabela_compras,
        "tabela_ranking": tabela_ranking,
        "tabela_parados": tabela_parados,
        "tabela_cliente_produtos": tabela_cliente_produtos,
        "resumo_cliente": resumo_cliente,
        "cliente_selecionado": cliente_selecionado,
        "tabela_fornecedores_comprados": tabela_fornecedores_comprados,
        "tabela_fornecedores_vendas": tabela_fornecedores_vendas,
        "fornecedores_em_ascensao": fornecedores_em_ascensao,
        "fornecedores_em_queda": fornecedores_em_queda,
        "resumo_fornecedores": resumo_fornecedores,
        "marcas_disponiveis": marcas_disponiveis,
        "clientes_disponiveis": clientes_disponiveis,
    }
