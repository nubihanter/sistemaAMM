# sales/dashboard_services.py
import base64
import json
import re
import uuid
from datetime import datetime, timedelta, date
import unicodedata
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.utils import PlotlyJSONEncoder
from .models import MetaVendedor, Vendedor
from inventory.models import ItemVenda, ProdutoEPI

USUARIO_PIPERUN_META_EMPRESA = "MARCELO NERIS"


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


def normalizar_nome(nome):
    if not nome:
        return ""
    nome_nfd = unicodedata.normalize("NFD", str(nome).upper())
    sem_acentos = "".join(c for c in nome_nfd if unicodedata.category(c) != "Mn")
    partes = sem_acentos.split()
    return partes[0] if partes else ""


def consolidar_clientes_por_documento(df: pd.DataFrame) -> pd.DataFrame:
    """
    Unifica variações de razão social/nome fantasia de um mesmo cliente usando o CNPJ/CPF (cliente_documento),
    adotando sempre o nome mais recente emitido para aquele documento.
    """
    if df.empty or "cliente_nome" not in df.columns:
        return df

    df = df.copy()
    df["cliente_nome"] = (
        df["cliente_nome"]
        .fillna("CLIENTE NÃO IDENTIFICADO")
        .astype(str)
        .str.strip()
        .replace({"": "CLIENTE NÃO IDENTIFICADO", "NAN": "CLIENTE NÃO IDENTIFICADO", "NONE": "CLIENTE NÃO IDENTIFICADO"})
    )

    if "cliente_documento" not in df.columns:
        return df

    def limpar_doc(val):
        if val is None or pd.isna(val):
            return ""
        d = re.sub(r"\D", "", str(val))
        return d if len(d) >= 8 else ""

    df["_doc_limpo"] = df["cliente_documento"].apply(limpar_doc)
    com_doc = df[df["_doc_limpo"] != ""]
    if not com_doc.empty:
        if "data_emissao" in com_doc.columns:
            com_doc = com_doc.sort_values("data_emissao")
        mapa_doc_nome = com_doc.groupby("_doc_limpo")["cliente_nome"].last().to_dict()
        mask_doc = df["_doc_limpo"] != ""
        df.loc[mask_doc, "cliente_nome"] = df.loc[mask_doc, "_doc_limpo"].map(mapa_doc_nome)

    df = df.drop(columns=["_doc_limpo"])
    return df


def obter_meta_vendedor(vendedora_selecionada, mes, ano):
    """
    Busca a meta mensal cruzando nome_hardness -> nome_piperun -> MetaVendedor.
    """
    if vendedora_selecionada == "EMPRESA":
        nome_piperun_alvo = normalizar_nome(USUARIO_PIPERUN_META_EMPRESA)
    else:
        vend = Vendedor.objects.filter(nome_hardness=vendedora_selecionada.strip().upper()).first()
        if vend and vend.nome_piperun:
            nome_piperun_alvo = normalizar_nome(vend.nome_piperun)
        else:
            nome_piperun_alvo = normalizar_nome(vendedora_selecionada)

    metas_mes = MetaVendedor.objects.filter(mes=mes, ano=ano)
    for m in metas_mes:
        if normalizar_nome(m.vendedor_nome) == nome_piperun_alvo:
            return float(m.valor)

    return 0.0


def obter_historico_metas_vendedor(vendedora_selecionada, mes_selecionado, ano_selecionado, limite=6):
    """
    Recupera as últimas metas cadastradas para o vendedor até o período selecionado.
    """
    if vendedora_selecionada == "EMPRESA":
        nome_piperun_alvo = normalizar_nome(USUARIO_PIPERUN_META_EMPRESA)
    else:
        vend = Vendedor.objects.filter(nome_hardness=vendedora_selecionada.strip().upper()).first()
        if vend and vend.nome_piperun:
            nome_piperun_alvo = normalizar_nome(vend.nome_piperun)
        else:
            nome_piperun_alvo = normalizar_nome(vendedora_selecionada)

    todas_metas = MetaVendedor.objects.all()
    metas_vendedor = [
        m
        for m in todas_metas
        if normalizar_nome(m.vendedor_nome) == nome_piperun_alvo
        and (m.ano < ano_selecionado or (m.ano == ano_selecionado and m.mes <= mes_selecionado))
    ]

    metas_vendedor.sort(key=lambda m: (m.ano, m.mes), reverse=True)
    ultimas_metas = metas_vendedor[:limite]
    ultimas_metas.sort(key=lambda m: (m.ano, m.mes))
    return ultimas_metas


def _calcular_margem_periodo(data_ini: date, data_fim: date, vendedora_selecionada: str) -> dict:
    """
    Calcula a margem bruta estimada do período cruzando ItemVenda com o custo unitário
    (na venda ou cadastro ProdutoEPI).
    """
    qs_itens = ItemVenda.objects.filter(data_emissao__gte=data_ini, data_emissao__lte=data_fim)
    if vendedora_selecionada and vendedora_selecionada != "EMPRESA":
        qs_itens = qs_itens.filter(vendedor_nome__iexact=vendedora_selecionada)

    itens_list = list(
        qs_itens.values(
            "codigo_produto",
            "quantidade",
            "valor_total",
            "valor_custo_unitario",
        )
    )
    if not itens_list:
        return {
            "tem_dados": False,
            "lucro_bruto": "R$ 0,00",
            "margem_pct": "0.0%",
            "custo_total": "R$ 0,00",
        }

    custos_catalogo = {
        p["sku"]: float(p["preco_custo"] or 0.0)
        for p in ProdutoEPI.objects.values("sku", "preco_custo")
    }

    receita_itens = 0.0
    custo_itens = 0.0
    for it in itens_list:
        qtd = float(it["quantidade"] or 0.0)
        val_tot = float(it["valor_total"] or 0.0)
        custo_un = float(it["valor_custo_unitario"] or 0.0)
        if custo_un <= 0:
            custo_un = custos_catalogo.get(it["codigo_produto"], 0.0)
        if custo_un <= 0 and qtd > 0:
            custo_un = (val_tot / qtd) * 0.65

        receita_itens += val_tot
        custo_itens += qtd * custo_un

    lucro_bruto = receita_itens - custo_itens
    margem_pct = (lucro_bruto / receita_itens * 100.0) if receita_itens > 0 else 0.0
    return {
        "tem_dados": True,
        "lucro_bruto": f"R$ {lucro_bruto:,.2f}",
        "margem_pct": f"{margem_pct:.1f}%",
        "custo_total": f"R$ {custo_itens:,.2f}",
    }


def gerar_metricas_e_graficos(
    df,
    vendedora_selecionada,
    mes_selecionado,
    ano_selecionado,
    incluir_margem_admin=False,
):
    metricas_vazias = {
        "total_vendas": "R$ 0,00",
        "num_vendas": 0,
        "num_clientes": 0,
        "ticket_medio": "R$ 0,00",
        "meta_total": "R$ 0,00",
        "meta_proporcional": "R$ 0,00",
        "percentual_meta": "0.0%",
        "percentual_ritmo": "0.0%",
        "status_meta": "Sem Meta",
        "status_class": "badge bg-secondary",
        "delta_ritmo": "",
        "dias_info": "",
        "projecao_fechamento": "R$ 0,00",
        "projecao_pct_meta": "0.0%",
        "mom_delta_str": "Sem base ant.",
        "mom_positivo": True,
        "yoy_delta_str": "Sem base YoY",
        "yoy_positivo": True,
        "margem_bruta_valor": "R$ 0,00",
        "margem_bruta_pct": "0.0%",
        "tem_margem": False,
    }

    if df.empty:
        return metricas_vazias, "", "", "", "", "", []

    df = df.copy()
    df["data_emissao"] = pd.to_datetime(df["data_emissao"])
    df["valor_total"] = pd.to_numeric(df["valor_total"], errors="coerce").fillna(0.0)
    df = consolidar_clientes_por_documento(df)

    # Período selecionado
    data_inicio = pd.Timestamp(year=ano_selecionado, month=mes_selecionado, day=1)
    if mes_selecionado == 12:
        data_fim = pd.Timestamp(year=ano_selecionado + 1, month=1, day=1) - timedelta(days=1)
    else:
        data_fim = pd.Timestamp(year=ano_selecionado, month=mes_selecionado + 1, day=1) - timedelta(days=1)

    # Mês imediatamente anterior (para MoM)
    if mes_selecionado == 1:
        mes_ant, ano_ant = 12, ano_selecionado - 1
    else:
        mes_ant, ano_ant = mes_selecionado - 1, ano_selecionado
    dt_ini_mom = date(ano_ant, mes_ant, 1)
    dt_fim_mom = (data_inicio - timedelta(days=1)).date()

    # Mesmo mês do ano anterior (para YoY)
    dt_ini_yoy = date(ano_selecionado - 1, mes_selecionado, 1)
    if mes_selecionado == 12:
        dt_fim_yoy = date(ano_selecionado, 1, 1) - timedelta(days=1)
    else:
        dt_fim_yoy = date(ano_selecionado - 1, mes_selecionado + 1, 1) - timedelta(days=1)

    # Filtro de dados da visão atual
    if vendedora_selecionada == "EMPRESA":
        df_vendedor = df.copy()
    else:
        df_vendedor = df[df["vendedor_nome"].astype(str).str.strip().str.upper() == vendedora_selecionada].copy()

    df_filtered = df_vendedor[
        (df_vendedor["data_emissao"].dt.date >= data_inicio.date())
        & (df_vendedor["data_emissao"].dt.date <= data_fim.date())
    ].copy()

    total_vendas = float(df_filtered["valor_total"].sum())
    num_vendas = len(df_filtered)
    num_clientes = int(df_filtered["cliente_nome"].nunique()) if not df_filtered.empty else 0
    ticket_medio = total_vendas / num_vendas if num_vendas > 0 else 0.0

    # Comparativo MoM e YoY
    vendas_mom = float(
        df_vendedor[
            (df_vendedor["data_emissao"].dt.date >= dt_ini_mom)
            & (df_vendedor["data_emissao"].dt.date <= dt_fim_mom)
        ]["valor_total"].sum()
    )
    vendas_yoy = float(
        df_vendedor[
            (df_vendedor["data_emissao"].dt.date >= dt_ini_yoy)
            & (df_vendedor["data_emissao"].dt.date <= dt_fim_yoy)
        ]["valor_total"].sum()
    )

    if vendas_mom > 0:
        mom_pct = ((total_vendas - vendas_mom) / vendas_mom) * 100.0
        sinal_mom = "+" if mom_pct >= 0 else ""
        mom_delta_str = f"{sinal_mom}{mom_pct:.1f}% vs {mes_ant:02d}/{ano_ant}"
        mom_positivo = mom_pct >= 0
    else:
        mom_delta_str = f"Sem base {mes_ant:02d}/{ano_ant}"
        mom_positivo = True

    if vendas_yoy > 0:
        yoy_pct = ((total_vendas - vendas_yoy) / vendas_yoy) * 100.0
        sinal_yoy = "+" if yoy_pct >= 0 else ""
        yoy_delta_str = f"{sinal_yoy}{yoy_pct:.1f}% vs {mes_selecionado:02d}/{ano_selecionado - 1}"
        yoy_positivo = yoy_pct >= 0
    else:
        yoy_delta_str = f"Sem base {mes_selecionado:02d}/{ano_selecionado - 1}"
        yoy_positivo = True

    # Meta do Vendedor / Empresa
    meta_periodo = obter_meta_vendedor(vendedora_selecionada, mes_selecionado, ano_selecionado)
    percentual_meta = (total_vendas / meta_periodo * 100.0) if meta_periodo > 0 else 0.0

    # --- CÁLCULO PONDERADO POR DIAS DECORRIDOS E PROJEÇÃO DE FECHAMENTO ---
    hoje = datetime.now().date()
    dias_no_mes = (data_fim - data_inicio).days + 1

    if ano_selecionado < hoje.year or (ano_selecionado == hoje.year and mes_selecionado < hoje.month):
        dias_decorridos = dias_no_mes
        mes_encerrado = True
    elif ano_selecionado > hoje.year or (ano_selecionado == hoje.year and mes_selecionado > hoje.month):
        dias_decorridos = 0
        mes_encerrado = False
    else:
        # Se estamos no mês corrente, usa hoje.day (ou o último dia com emissão se maior)
        dias_decorridos = min(max(hoje.day, 1), dias_no_mes)
        mes_encerrado = False

    proporcao_decorrida = (dias_decorridos / dias_no_mes) if dias_no_mes > 0 else 1.0
    meta_proporcional = meta_periodo * proporcao_decorrida

    if mes_encerrado:
        projecao_fechamento_val = total_vendas
    elif dias_decorridos > 0:
        projecao_fechamento_val = (total_vendas / dias_decorridos) * dias_no_mes
    else:
        projecao_fechamento_val = 0.0

    projecao_pct_meta = (projecao_fechamento_val / meta_periodo * 100.0) if meta_periodo > 0 else 0.0
    percentual_ritmo = (
        (total_vendas / meta_proporcional * 100.0)
        if meta_proporcional > 0
        else (100.0 if percentual_meta >= 100 else 0.0)
    )

    if meta_periodo > 0:
        if percentual_meta >= 100:
            status_meta = "✅ META ATINGIDA"
            status_class = "badge bg-success"
        elif percentual_ritmo >= 100:
            status_meta = f"✅ NO RITMO DA META ({percentual_ritmo:.1f}%)"
            status_class = "badge bg-success"
        elif percentual_ritmo >= 80:
            status_meta = f"⚠️ PRÓXIMO DO RITMO ({percentual_ritmo:.1f}%)"
            status_class = "badge bg-warning text-dark"
        else:
            status_meta = f"❌ ABAIXO DO RITMO ({percentual_ritmo:.1f}%)"
            status_class = "badge bg-danger"
    else:
        status_meta = "Sem Meta Definida"
        status_class = "badge bg-secondary"

    delta_ritmo = (
        f"{percentual_ritmo:.1f}% do ritmo esperado"
        if (proporcao_decorrida < 1.0 and dias_decorridos > 0)
        else ""
    )
    dias_info = f"Dia {dias_decorridos}/{dias_no_mes} (Meta até hoje: R$ {meta_proporcional:,.2f})"

    dados_margem = {"tem_dados": False, "lucro_bruto": "R$ 0,00", "margem_pct": "0.0%"}
    if incluir_margem_admin:
        dados_margem = _calcular_margem_periodo(
            data_ini=data_inicio.date(),
            data_fim=data_fim.date(),
            vendedora_selecionada=vendedora_selecionada,
        )

    metricas = {
        "total_vendas": f"R$ {total_vendas:,.2f}",
        "num_vendas": num_vendas,
        "num_clientes": num_clientes,
        "ticket_medio": f"R$ {ticket_medio:,.2f}",
        "meta_total": f"R$ {meta_periodo:,.2f}",
        "meta_proporcional": f"R$ {meta_proporcional:,.2f}",
        "percentual_meta": f"{percentual_meta:.1f}%",
        "percentual_ritmo": f"{percentual_ritmo:.1f}%",
        "status_meta": status_meta,
        "status_class": status_class,
        "delta_ritmo": delta_ritmo,
        "dias_info": dias_info,
        "projecao_fechamento": f"R$ {projecao_fechamento_val:,.2f}",
        "projecao_pct_meta": f"{projecao_pct_meta:.1f}%",
        "mes_encerrado": mes_encerrado,
        "mom_delta_str": mom_delta_str,
        "mom_positivo": mom_positivo,
        "yoy_delta_str": yoy_delta_str,
        "yoy_positivo": yoy_positivo,
        "margem_bruta_valor": dados_margem["lucro_bruto"],
        "margem_bruta_pct": dados_margem["margem_pct"],
        "tem_margem": dados_margem["tem_dados"],
    }

    # Gráfico 1: Evolução Diária Acumulada vs Rampa Ideal da Meta
    grafico_evolucao_html = ""
    grafico_barras_qtd_html = ""
    grafico_top_clientes_html = ""

    if not df_filtered.empty:
        df_filtered["Data"] = df_filtered["data_emissao"].dt.date
        df_diario = df_filtered.groupby("Data").agg({"valor_total": ["sum", "count"]}).reset_index()
        df_diario.columns = ["Data", "Valor", "Quantidade"]
        df_diario = df_diario.sort_values("Data")
        df_diario["Valor Acumulado"] = df_diario["Valor"].cumsum()

        fig_linha = go.Figure()
        fig_linha.add_trace(
            go.Scatter(
                x=df_diario["Data"],
                y=df_diario["Valor Acumulado"],
                mode="lines+markers",
                name="Realizado Acumulado",
                line=dict(color="#1f77b4", width=3),
            )
        )
        if meta_periodo > 0:
            fig_linha.add_hline(
                y=meta_periodo,
                line_dash="dash",
                line_color="#15803d",
                annotation_text=f"Meta Mensal (R$ {meta_periodo:,.0f})",
                annotation_position="top left",
            )
            # Linha de Ritmo Ideal (Rampa linear do dia 1 ao último dia do mês)
            fig_linha.add_trace(
                go.Scatter(
                    x=[data_inicio.date(), data_fim.date()],
                    y=[meta_periodo / dias_no_mes, meta_periodo],
                    mode="lines",
                    name="Ritmo Ideal da Meta",
                    line=dict(color="#94a3b8", width=2, dash="dot"),
                )
            )

        fig_linha.update_layout(
            title="📈 Evolução Diária Acumulada vs Ritmo da Meta (R$)",
            height=360,
            margin=dict(t=45, b=20, l=20, r=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            yaxis_title="Faturamento Acumulado (R$)",
            xaxis_title="Data",
        )
        grafico_evolucao_html = fig_to_html(fig_linha)

        fig_barras = px.bar(
            df_diario,
            x="Data",
            y="Quantidade",
            text="Quantidade",
            title="📦 Nº de Notas Fiscais Emitidas por Dia",
        )
        fig_barras.update_traces(marker_color="#0284c7")
        fig_barras.update_layout(height=360, margin=dict(t=45, b=20, l=20, r=20))
        grafico_barras_qtd_html = fig_to_html(fig_barras)

        # Top 10 Clientes do Mês na visão selecionada
        df_top_cli = (
            df_filtered.groupby("cliente_nome")
            .agg(Total=("valor_total", "sum"), Pedidos=("numero_nota", "nunique"))
            .reset_index()
            .sort_values("Total", ascending=False)
            .head(10)
        )
        if not df_top_cli.empty:
            df_top_cli["Cliente_Curto"] = df_top_cli["cliente_nome"].astype(str).str.slice(0, 32)
            fig_top_cli = px.bar(
                df_top_cli,
                x="Total",
                y="Cliente_Curto",
                orientation="h",
                text_auto=".2s",
                title=f"🏅 Top 10 Clientes do Mês ({mes_selecionado:02d}/{ano_selecionado})",
                labels={"Total": "Faturamento (R$)", "Cliente_Curto": "Cliente"},
            )
            fig_top_cli.update_traces(marker_color="#15803d")
            fig_top_cli.update_layout(
                height=360,
                margin=dict(t=45, b=20, l=20, r=20),
                yaxis=dict(categoryorder="total ascending"),
            )
            grafico_top_clientes_html = fig_to_html(fig_top_cli)

    # Gráfico 2: Ranking por % da Meta (Visível inclusive para Vendedor para estimular competição)
    grafico_ranking_html = ""
    df_periodo_geral = df[
        (df["data_emissao"].dt.date >= data_inicio.date())
        & (df["data_emissao"].dt.date <= data_fim.date())
    ].copy()

    ranking_data = []
    if "vendedor_nome" in df.columns:
        vendedores_permitidos_ranking = set(
            Vendedor.objects.filter(ativo=True, ativo_ranking=True).values_list("nome_hardness", flat=True)
        )
        vendedores_ranking = [
            str(v).strip().upper()
            for v in df["vendedor_nome"].dropna().unique()
            if str(v).strip().upper() not in ["", "NAN", "NONE"]
            and str(v).strip().upper() in vendedores_permitidos_ranking
        ]

        for vend in vendedores_ranking:
            total_vend = float(
                df_periodo_geral[
                    df_periodo_geral["vendedor_nome"].astype(str).str.strip().str.upper() == vend
                ]["valor_total"].sum()
            )
            meta_vend = obter_meta_vendedor(vend, mes_selecionado, ano_selecionado)
            pct_meta = (total_vend / meta_vend * 100.0) if meta_vend > 0 else 0.0

            ranking_data.append(
                {
                    "Posição": 0,
                    "Vendedor": vend,
                    "% Meta": pct_meta,
                    "Total_Vendas": total_vend,
                    "Meta": meta_vend,
                    "tem_meta": meta_vend > 0,
                }
            )

    if ranking_data:
        df_rank = pd.DataFrame(ranking_data)
        df_com_meta = (
            df_rank[df_rank["tem_meta"]].copy().sort_values("% Meta", ascending=False).reset_index(drop=True)
        )
        df_sem_meta = (
            df_rank[~df_rank["tem_meta"]].copy().sort_values("Vendedor", ascending=True).reset_index(drop=True)
        )

        df_com_meta["Posição"] = range(1, len(df_com_meta) + 1)
        df_sem_meta["Posição"] = range(
            len(df_com_meta) + 1, len(df_com_meta) + len(df_sem_meta) + 1
        )

        df_ranking_final = pd.concat([df_com_meta, df_sem_meta], ignore_index=True)

        def formata_nome(row):
            pos = int(row["Posição"])
            nome = row["Vendedor"]
            if pos == 1:
                return f"🥇 {nome}"
            elif pos == 2:
                return f"🥈 {nome}"
            elif pos == 3:
                return f"🥉 {nome}"
            return f"{pos}º {nome}"

        def define_cor(pos):
            if pos == 1:
                return "#ffd700"
            elif pos == 2:
                return "#c0c0c0"
            elif pos == 3:
                return "#cd7f32"
            return "#1f77b4"

        df_ranking_final["Nome_Display"] = df_ranking_final.apply(formata_nome, axis=1)
        df_ranking_final["Cor"] = df_ranking_final["Posição"].apply(define_cor)

        # Exibe apenas % da Meta no hover para preservar sigilo de valores entre vendedores
        fig_ranking = px.bar(
            df_ranking_final,
            x="Nome_Display",
            y="% Meta",
            title=f"🏆 Ranking de Vendas - % da Meta Atingida ({data_inicio.strftime('%m/%Y')})",
            labels={"% Meta": "% da Meta", "Nome_Display": "Vendedor"},
            color="Cor",
            color_discrete_map="identity",
            text="% Meta",
            hover_data={"Cor": False, "% Meta": ":.1f"},
        )
        fig_ranking.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig_ranking.update_layout(
            xaxis_tickangle=-35,
            yaxis=dict(
                ticksuffix="%",
                range=[0, max(float(df_ranking_final["% Meta"].dropna().max()) * 1.18, 105.0)],
            ),
            showlegend=False,
            height=400,
            margin=dict(t=50, b=90, l=20, r=20),
        )
        grafico_ranking_html = fig_to_html(fig_ranking)

    # Gráfico 3: Histórico de Metas vs Realizado (Últimas 6 Metas)
    grafico_historico_metas_html = ""
    ultimas_metas_banco = obter_historico_metas_vendedor(
        vendedora_selecionada, mes_selecionado, ano_selecionado, limite=6
    )
    historico_plot_data = []

    for m in ultimas_metas_banco:
        dt_m_ini = pd.Timestamp(year=m.ano, month=m.mes, day=1)
        if m.mes == 12:
            dt_m_fim = pd.Timestamp(year=m.ano + 1, month=1, day=1) - timedelta(days=1)
        else:
            dt_m_fim = pd.Timestamp(year=m.ano, month=m.mes + 1, day=1) - timedelta(days=1)

        vendas_m = df_vendedor[
            (df_vendedor["data_emissao"].dt.date >= dt_m_ini.date())
            & (df_vendedor["data_emissao"].dt.date <= dt_m_fim.date())
        ]["valor_total"].sum()

        periodo_label = f"{m.mes:02d}/{m.ano}"
        historico_plot_data.append({"Período": periodo_label, "Tipo": "Meta", "Valor": float(m.valor)})
        historico_plot_data.append({"Período": periodo_label, "Tipo": "Realizado", "Valor": float(vendas_m)})

    if not historico_plot_data and meta_periodo > 0:
        periodo_label = f"{mes_selecionado:02d}/{ano_selecionado}"
        historico_plot_data.append({"Período": periodo_label, "Tipo": "Meta", "Valor": float(meta_periodo)})
        historico_plot_data.append({"Período": periodo_label, "Tipo": "Realizado", "Valor": float(total_vendas)})

    if historico_plot_data:
        df_compare = pd.DataFrame(historico_plot_data)
        fig_compare = px.bar(
            df_compare,
            x="Período",
            y="Valor",
            color="Tipo",
            barmode="group",
            title="🎯 Histórico de Metas vs Realizado",
            labels={"Valor": "Valor (R$)", "Período": "Mês/Ano"},
            color_discrete_map={"Meta": "#1f77b4", "Realizado": "#ff7f0e"},
            text_auto=".2s",
        )
        fig_compare.update_layout(height=360, margin=dict(t=45, b=20, l=20, r=20))
        grafico_historico_metas_html = fig_to_html(fig_compare)

    # Tabela de Notas Fiscais do Período
    tabela_notas = []
    if not df_filtered.empty:
        df_nf_tab = df_filtered.sort_values(["data_emissao", "numero_nota"], ascending=[False, False]).copy()
        df_nf_tab["data_str"] = df_nf_tab["data_emissao"].dt.strftime("%d/%m/%Y")
        df_nf_tab["data_iso"] = df_nf_tab["data_emissao"].dt.strftime("%Y-%m-%d")
        tabela_notas = df_nf_tab[
            ["numero_nota", "cliente_nome", "vendedor_nome", "data_str", "data_iso", "valor_total"]
        ].to_dict(orient="records")

    return (
        metricas,
        grafico_evolucao_html,
        grafico_barras_qtd_html,
        grafico_ranking_html,
        grafico_historico_metas_html,
        grafico_top_clientes_html,
        tabela_notas,
    )


FATURAMENTO_MINIMO_INATIVIDADE = 500.0


def calcular_ticket_medio_6m_clientes(df):
    """
    Calcula o faturamento médio mensal retroativo à ÚLTIMA VENDA de cada cliente.
    Se o cliente tiver histórico menor que 6 meses, divide apenas pelos meses decorridos.
    """
    ticket_map = {}
    df_ordenado = df.sort_values("data_emissao")

    for cliente, grupo in df_ordenado.groupby("cliente_nome"):
        ult_data = grupo["data_emissao"].max()
        prim_data = grupo["data_emissao"].min()

        janela_inicio = ult_data - pd.Timedelta(days=180)
        vendas_janela = grupo[grupo["data_emissao"] >= janela_inicio]["valor_total"].sum()

        if prim_data <= janela_inicio:
            meses_divisor = 6.0
        else:
            meses_decorridos = (ult_data.year - prim_data.year) * 12 + (ult_data.month - prim_data.month) + 1
            meses_divisor = min(6.0, max(1.0, float(meses_decorridos)))

        ticket_map[cliente] = float(vendas_janela) / meses_divisor

    return ticket_map


def gerar_analise_clientes(df, vendedora_selecionada="EMPRESA", filtros_curva=None, filtros_status=None):
    """
    Gera KPIs, gráficos de risco e a tabela analítica de clientes.
    - Unifica clientes duplicados por CNPJ/CPF (cliente_documento).
    - Quando filtrado por um Vendedor específico, considera APENAS os clientes cuja
      ÚLTIMA venda foi realizada por aquele vendedor (dono atual da carteira).
    """
    if df.empty or "cliente_nome" not in df.columns:
        return {}, "", "", pd.DataFrame()

    df = df.copy()
    df["data_emissao"] = pd.to_datetime(df["data_emissao"])
    df["valor_total"] = pd.to_numeric(df["valor_total"], errors="coerce").fillna(0.0).astype(float)
    df = consolidar_clientes_por_documento(df)

    # 1. Histórico GLOBAL de cada cliente ordenado por data crescente
    df_ordenado_global = df.sort_values(["data_emissao", "valor_total"])

    def get_ultimo_vendedor(serie):
        """Retorna o vendedor da última venda, ignorando valores nulos ou vazios."""
        validos = serie.dropna()
        validos = validos[
            ~validos.astype(str).str.strip().str.upper().isin(["", "NAN", "NONE", "DESCONHECIDO"])
        ]
        return str(validos.iloc[-1]).strip().upper() if not validos.empty else "-"

    df_clientes = (
        df_ordenado_global.groupby("cliente_nome")
        .agg(
            Faturamento_Total=("valor_total", "sum"),
            Num_Vendas=("valor_total", "count"),
            Ultima_Venda=("data_emissao", "max"),
            Primeira_Venda=("data_emissao", "min"),
            Vendedora=("vendedor_nome", get_ultimo_vendedor),
        )
        .reset_index()
    )

    # 2. Cálculo do Ticket Médio 6m Individual por Cliente
    ticket_6m_map = calcular_ticket_medio_6m_clientes(df)

    # 3. Filtragem da Carteira pelo Dono Atual do Cliente (Vendedor da ÚLTIMA venda)
    if vendedora_selecionada and vendedora_selecionada != "EMPRESA":
        df_clientes = df_clientes[df_clientes["Vendedora"] == vendedora_selecionada.strip().upper()].copy()

    # Filtro de faturamento mínimo acumulado
    df_clientes = df_clientes[df_clientes["Faturamento_Total"] >= FATURAMENTO_MINIMO_INATIVIDADE].copy()
    if df_clientes.empty:
        return {}, "", "", pd.DataFrame()

    data_maxima_base = df["data_emissao"].max()
    ano_vigente = data_maxima_base.year

    df_clientes["Dias_Inatividade"] = (data_maxima_base - df_clientes["Ultima_Venda"]).dt.days.fillna(0).astype(int)
    df_clientes["Ticket_Medio_6m"] = df_clientes["cliente_nome"].map(ticket_6m_map).fillna(0.0).astype(float)
    df_clientes["Faturamento_Total"] = df_clientes["Faturamento_Total"].astype(float)

    def classificar_curva(ticket):
        if ticket >= 5000:
            return "AA"
        elif ticket >= 3000:
            return "A"
        elif ticket >= 1500:
            return "B"
        elif ticket >= 700:
            return "C"
        return "D"

    df_clientes["Curva"] = df_clientes["Ticket_Medio_6m"].apply(classificar_curva)

    def definir_status(row):
        if row["Primeira_Venda"].year == ano_vigente:
            return "Novo"
        elif row["Dias_Inatividade"] <= 30:
            return "Ativo"
        elif row["Dias_Inatividade"] <= 90:
            return "Em Risco"
        return "Inativo"

    df_clientes["Status"] = df_clientes.apply(definir_status, axis=1)

    curvas_aplicadas = filtros_curva or ["AA", "A", "B"]
    status_aplicados = filtros_status or ["Novo", "Ativo", "Em Risco", "Inativo"]
    df_filtrado = df_clientes[
        df_clientes["Curva"].isin(curvas_aplicadas) & df_clientes["Status"].isin(status_aplicados)
    ].copy()

    # 5. KPIs da Carteira
    kpis_carteira = {
        "total_clientes": len(df_clientes),
        "clientes_ativos": len(df_clientes[df_clientes["Status"] == "Ativo"]),
        "clientes_risco": len(df_clientes[df_clientes["Status"] == "Em Risco"]),
        "clientes_inativos": len(df_clientes[df_clientes["Status"] == "Inativo"]),
        "clientes_novos": len(df_clientes[df_clientes["Status"] == "Novo"]),
        "prioritarios": len(df_clientes[df_clientes["Curva"].isin(["AA", "A", "B"])]),
        "faturamento_carteira": f"R$ {df_clientes['Faturamento_Total'].sum():,.2f}",
        "potencial_risco_mensal": f"R$ {df_clientes[df_clientes['Status'] == 'Em Risco']['Ticket_Medio_6m'].sum():,.2f}",
    }

    # 6. Gráficos de Prioridade
    grafico_status_html = ""
    grafico_matriz_html = ""
    df_prioridade = df_filtrado

    if not df_prioridade.empty:
        fig_status = px.histogram(
            df_prioridade,
            x="Status",
            color="Curva",
            title="🎯 Clientes na Seleção por Status e Curva",
            category_orders={
                "Status": ["Novo", "Ativo", "Em Risco", "Inativo"],
                "Curva": ["AA", "A", "B", "C", "D"],
            },
            color_discrete_map={
                "AA": "#00441b",
                "A": "#238b45",
                "B": "#74c476",
                "C": "#bae4b3",
                "D": "#cbd5e1",
            },
            barmode="group",
            text_auto=True,
        )
        fig_status.update_layout(yaxis_title="Qtd Clientes", height=380, margin=dict(t=40, b=20, l=20, r=20))
        grafico_status_html = fig_to_html(fig_status)

        df_prioridade["Tamanho_Bolha"] = df_prioridade["Faturamento_Total"].clip(lower=100.0)

        fig_scatter = px.scatter(
            df_prioridade,
            x="Dias_Inatividade",
            y="Ticket_Medio_6m",
            color="Status",
            size="Tamanho_Bolha",
            size_max=28,
            hover_name="cliente_nome",
            hover_data={
                "Tamanho_Bolha": False,
                "Faturamento_Total": ":.2f",
                "Dias_Inatividade": True,
                "Ticket_Medio_6m": ":.2f",
                "Vendedora": True,
                "Curva": True,
            },
            title="⚠️ Matriz de Risco: Dias sem Comprar vs Fat. Médio Mensal (6m)",
            labels={"Dias_Inatividade": "Dias sem comprar", "Ticket_Medio_6m": "Fat. Médio Mensal (6m)"},
            category_orders={"Status": ["Novo", "Ativo", "Em Risco", "Inativo"]},
            color_discrete_map={
                "Novo": "#17becf",
                "Ativo": "#2ca02c",
                "Em Risco": "#ff7f0e",
                "Inativo": "#d62728",
            },
        )
        fig_scatter.update_traces(
            marker=dict(
                sizemode="area",
                sizeref=2.0 * max(df_prioridade["Tamanho_Bolha"]) / (40.0**2),
                sizemin=4,
            )
        )
        fig_scatter.add_vline(
            x=30, line_dash="dash", line_color="green", annotation_text="Ativos (≤30d)", annotation_position="top left"
        )
        fig_scatter.add_vline(
            x=90, line_dash="dash", line_color="red", annotation_text="Inativos (>90d)", annotation_position="top right"
        )
        fig_scatter.update_layout(height=380, margin=dict(t=40, b=20, l=20, r=20))
        grafico_matriz_html = fig_to_html(fig_scatter)

    # 7. Preparação da Tabela Analítica
    df_tabela = df_filtrado.copy()
    df_tabela["Ultima_Venda_str"] = df_tabela["Ultima_Venda"].dt.strftime("%d/%m/%Y")
    df_tabela["Ultima_Venda_iso"] = df_tabela["Ultima_Venda"].dt.strftime("%Y-%m-%d")

    ordem_map = {"AA": 1, "A": 2, "B": 3, "C": 4, "D": 5}
    df_tabela["ordem_curva"] = df_tabela["Curva"].map(ordem_map)
    df_tabela = df_tabela.sort_values(
        by=["ordem_curva", "Dias_Inatividade"], ascending=[True, False]
    ).drop(columns=["ordem_curva"])

    return kpis_carteira, grafico_status_html, grafico_matriz_html, df_tabela