# sales/dashboard_services.py
from datetime import datetime, timedelta, date
import unicodedata
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from django.core.cache import cache
from .models import MetaVendedor, Vendedor

from integrations.piperun import PipeRunAPI

USUARIO_PIPERUN_META_EMPRESA = "MARCELO NERIS"
VENDEDORES_OCULTOS = ["DESCONHECIDO", "LETICIA", "THIAGO", "VERONICA", "LENIRA", "RODRIGO", "ROBSON"]
EXCLUDE_FROM_RANKING = ["MARCELO", "INANJARA", "JUSLIENE", "MAYARA", "ANDRE", "WILMA"]


def normalizar_nome(nome):
    if not nome:
        return ""
    nome_nfd = unicodedata.normalize('NFD', str(nome).upper())
    sem_acentos = ''.join(c for c in nome_nfd if unicodedata.category(c) != 'Mn')
    return ' '.join(sem_acentos.split()).split()[0]


def obter_meta_vendedor(vendedora_selecionada, mes, ano):
    """
    Busca a meta mensal cruzando nome_hardness -> nome_piperun -> MetaVendedor.
    """
    if vendedora_selecionada == "EMPRESA":
        nome_piperun_alvo = normalizar_nome(USUARIO_PIPERUN_META_EMPRESA)
    else:
        # Busca o cadastro do vendedor pelo nome do Hardness
        vend = Vendedor.objects.filter(nome_hardness=vendedora_selecionada.strip().upper()).first()
        if vend and vend.nome_piperun:
            nome_piperun_alvo = normalizar_nome(vend.nome_piperun)
        else:
            # Fallback caso ainda não esteja vinculado: usa o próprio nome normalizado
            nome_piperun_alvo = normalizar_nome(vendedora_selecionada)

    # Busca a meta cadastrada no banco para o mês/ano
    metas_mes = MetaVendedor.objects.filter(mes=mes, ano=ano)
    for m in metas_mes:
        if normalizar_nome(m.vendedor_nome) == nome_piperun_alvo:
            return float(m.valor)

    return 0.0

def gerar_metricas_e_graficos(df, vendedora_selecionada, mes_selecionado, ano_selecionado):
    metricas_vazias = {
        "total_vendas": "R$ 0,00",
        "num_vendas": 0,
        "num_clientes": 0,
        "ticket_medio": "R$ 0,00",
        "meta_total": "R$ 0,00",
        "percentual_meta": "0.0%",
        "status_meta": "Sem Meta"
    }

    if df.empty:
        return metricas_vazias, "", "", ""

    df['data_emissao'] = pd.to_datetime(df['data_emissao'])
    df['valor_total'] = pd.to_numeric(df['valor_total'], errors='coerce').fillna(0)

    # Período
    data_inicio = pd.Timestamp(year=ano_selecionado, month=mes_selecionado, day=1)
    if mes_selecionado == 12:
        data_fim = pd.Timestamp(year=ano_selecionado + 1, month=1, day=1) - timedelta(days=1)
    else:
        data_fim = pd.Timestamp(year=ano_selecionado, month=mes_selecionado + 1, day=1) - timedelta(days=1)


    # Filtro de dados da visão atual
    if vendedora_selecionada == "EMPRESA":
        df_vendedor = df.copy()
    else:
        df_vendedor = df[df['vendedor_nome'] == vendedora_selecionada].copy()

    df_filtered = df_vendedor[
        (df_vendedor['data_emissao'].dt.date >= data_inicio.date()) &
        (df_vendedor['data_emissao'].dt.date <= data_fim.date())
    ].copy()

    total_vendas = float(df_filtered['valor_total'].sum())
    num_vendas = len(df_filtered)
    num_clientes = df_filtered['cliente_nome'].nunique()
    ticket_medio = total_vendas / num_vendas if num_vendas > 0 else 0.0

    # Meta do Vendedor / Empresa
    meta_periodo = obter_meta_vendedor(vendedora_selecionada, mes_selecionado, ano_selecionado)
    percentual_meta = (total_vendas / meta_periodo * 100) if meta_periodo > 0 else 0.0

    if meta_periodo > 0:
        if percentual_meta >= 100:
            status_meta = "✅ META ATINGIDA"
        elif percentual_meta >= 80:
            status_meta = "⚠️ PRÓXIMO DA META"
        else:
            status_meta = "❌ ABAIXO DA META"
    else:
        status_meta = "Sem Meta Definida"

    metricas = {
        "total_vendas": f"R$ {total_vendas:,.2f}",
        "num_vendas": num_vendas,
        "num_clientes": num_clientes,
        "ticket_medio": f"R$ {ticket_medio:,.2f}",
        "meta_total": f"R$ {meta_periodo:,.2f}",
        "percentual_meta": f"{percentual_meta:.1f}%",
        "status_meta": status_meta
    }

    # Gráfico 1: Evolução Diária
    grafico_evolucao_html = ""
    grafico_barras_qtd_html = ""
    if not df_filtered.empty:
        df_filtered['Data'] = df_filtered['data_emissao'].dt.date
        df_diario = df_filtered.groupby('Data').agg({'valor_total': ['sum', 'count']}).reset_index()
        df_diario.columns = ['Data', 'Valor', 'Quantidade']
        df_diario = df_diario.sort_values('Data')

        fig_linha = px.line(df_diario, x='Data', y='Valor', markers=True, title="Evolução Diária (R$)")
        fig_linha.update_layout(height=350, margin=dict(t=40, b=20, l=20, r=20))
        grafico_evolucao_html = fig_linha.to_html(full_html=False, include_plotlyjs=False)

        fig_barras = px.bar(df_diario, x='Data', y='Quantidade', title="Nº Vendas por Dia")
        fig_barras.update_layout(height=350, margin=dict(t=40, b=20, l=20, r=20))
        grafico_barras_qtd_html = fig_barras.to_html(full_html=False, include_plotlyjs=False)

    # Gráfico 2: Ranking por % da Meta (Lógica Fiel ao Streamlit)
    grafico_ranking_html = ""
    df_periodo_geral = df[
        (df['data_emissao'].dt.date >= data_inicio.date()) &
        (df['data_emissao'].dt.date <= data_fim.date())
    ].copy()

    ranking_data = []
    if 'vendedor_nome' in df.columns:
        vendedores_ranking = [
            v for v in df['vendedor_nome'].dropna().unique()
            if v and v not in VENDEDORES_OCULTOS and v not in EXCLUDE_FROM_RANKING
        ]

        for vend in vendedores_ranking:
            total_vend = float(df_periodo_geral[df_periodo_geral['vendedor_nome'] == vend]['valor_total'].sum())
            meta_vend = obter_meta_vendedor(vend, mes_selecionado, ano_selecionado)
            pct_meta = (total_vend / meta_vend * 100) if meta_vend > 0 else 0.0

            ranking_data.append({
                "Posição": 0,
                "Vendedora": vend,
                "% Meta": pct_meta,
                "Total_Vendas": total_vend,
                "Meta": meta_vend,
                "tem_meta": meta_vend > 0
            })

    if ranking_data:
        df_rank = pd.DataFrame(ranking_data)
        df_com_meta = df_rank[df_rank['tem_meta']].copy().sort_values('% Meta', ascending=False).reset_index(drop=True)
        df_sem_meta = df_rank[~df_rank['tem_meta']].copy().sort_values('Vendedora', ascending=True).reset_index(drop=True)

        df_com_meta['Posição'] = range(1, len(df_com_meta) + 1)
        df_sem_meta['Posição'] = range(len(df_com_meta) + 1, len(df_com_meta) + len(df_sem_meta) + 1)

        df_ranking_final = pd.concat([df_com_meta, df_sem_meta], ignore_index=True)

        def formata_nome(row):
            pos = int(row['Posição'])
            nome = row['Vendedora']
            if pos == 1:
                return f"🥇 {nome}"
            elif pos == 2:
                return f"🥈 {nome}"
            elif pos == 3:
                return f"🥉 {nome}"
            return f"{pos}º {nome}"

        def define_cor(pos):
            if pos == 1:
                return "#ffd700"  # Ouro
            elif pos == 2:
                return "#c0c0c0"  # Prata
            elif pos == 3:
                return "#cd7f32"  # Bronze
            return "#1f77b4"     # Padrão

        df_ranking_final['Nome_Display'] = df_ranking_final.apply(formata_nome, axis=1)
        df_ranking_final['Cor'] = df_ranking_final['Posição'].apply(define_cor)

        fig_ranking = px.bar(
            df_ranking_final,
            x='Nome_Display',
            y='% Meta',
            title=f"🏆 Ranking de Vendedoras - % da Meta Atingida ({data_inicio.strftime('%m/%Y')})",
            labels={'% Meta': '% da Meta', 'Nome_Display': 'Vendedora'},
            color='Cor',
            color_discrete_map='identity',
            text='% Meta'
        )

        fig_ranking.update_traces(texttemplate='%{text:.1f}%', textposition='outside')
        fig_ranking.update_layout(
            xaxis_tickangle=-45,
            yaxis=dict(ticksuffix="%"),
            showlegend=False,
            height=450,
            margin=dict(t=50, b=100, l=20, r=20)
        )
        grafico_ranking_html = fig_ranking.to_html(full_html=False, include_plotlyjs=False)

    return metricas, grafico_evolucao_html, grafico_barras_qtd_html, grafico_ranking_html