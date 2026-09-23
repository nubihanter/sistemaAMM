import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
from datetime import datetime, timedelta
import unicodedata

VENDEDORES_OCULTOS = ["DESCONHECIDO", "LETICIA", "THIAGO", "VERONICA", "LENIRA", "RODRIGO", "ROBSON"]
EXCLUDE_FROM_RANKING = ["MARCELO", "INANJARA", "JUSLIENE", "MAYARA", "ANDRE", "WILMA"]

def normalizar_nome(nome):
    if not nome:
        return ""
    nome_nfd = unicodedata.normalize('NFD', str(nome).upper())
    sem_acentos = ''.join(c for c in nome_nfd if unicodedata.category(c) != 'Mn')
    return ' '.join(sem_acentos.split()).split()[0]

def gerar_metricas_e_graficos(df, vendedora_selecionada, mes_selecionado, ano_selecionado):
    if df.empty:
        return {}, "", "", "", ""

    # Converte tipos
    df['data_emissao'] = pd.to_datetime(df['data_emissao'])
    df['valor_total'] = pd.to_numeric(df['valor_total'], errors='coerce').fillna(0)

    # Filtra por vendedor se não for EMPRESA
    if vendedora_selecionada == "EMPRESA":
        df_vendedor = df.copy()
    else:
        df_vendedor = df[df['vendedor_nome'] == vendedora_selecionada].copy()

    # Período
    data_inicio = pd.Timestamp(year=ano_selecionado, month=mes_selecionado, day=1)
    if mes_selecionado == 12:
        data_fim = pd.Timestamp(year=ano_selecionado+1, month=1, day=1) - timedelta(days=1)
    else:
        data_fim = pd.Timestamp(year=ano_selecionado, month=mes_selecionado+1, day=1) - timedelta(days=1)

    df_filtered = df_vendedor[
        (df_vendedor['data_emissao'].dt.date >= data_inicio.date()) &
        (df_vendedor['data_emissao'].dt.date <= data_fim.date())
    ].copy()

    # Métricas
    total_vendas = df_filtered['valor_total'].sum()
    num_vendas = len(df_filtered)
    num_clientes = df_filtered['cliente_nome'].nunique()
    ticket_medio = total_vendas / num_vendas if num_vendas > 0 else 0

    metricas = {
        "total_vendas": f"R$ {total_vendas:,.2f}",
        "num_vendas": num_vendas,
        "num_clientes": num_clientes,
        "ticket_medio": f"R$ {ticket_medio:,.2f}"
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

    # Gráfico 2: Ranking
    df_periodo_geral = df[
        (df['data_emissao'].dt.date >= data_inicio.date()) &
        (df['data_emissao'].dt.date <= data_fim.date())
    ].copy()
    
    ranking_data = []
    vendedores_ranking = [v for v in df['vendedor_nome'].dropna().unique() if v not in VENDEDORES_OCULTOS and v not in EXCLUDE_FROM_RANKING]
    for vend in vendedores_ranking:
        total = df_periodo_geral[df_periodo_geral['vendedor_nome'] == vend]['valor_total'].sum()
        ranking_data.append({"Vendedor": vend, "Total": total})

    df_rank = pd.DataFrame(ranking_data).sort_values("Total", ascending=False)
    fig_rank = px.bar(df_rank, x='Vendedor', y='Total', text_auto='.2s', title="Ranking de Vendas do Mês (R$)")
    fig_rank.update_layout(height=380, margin=dict(t=40, b=50, l=20, r=20))
    grafico_ranking_html = fig_rank.to_html(full_html=False, include_plotlyjs=False)

    return metricas, grafico_evolucao_html, grafico_barras_qtd_html, grafico_ranking_html