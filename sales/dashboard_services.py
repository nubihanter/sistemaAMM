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
        df_diario["Valor Acumulado"] = df_diario["Valor"].cumsum()

        fig_linha = px.line(df_diario, x='Data', y='Valor Acumulado', markers=True, title="Evolução Diária (R$)")
        fig_linha.add_hline(y=meta_periodo, line_dash="dash", line_color="green", annotation_text="Meta do Mês", annotation_position="top left")
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
        # Busca no banco os vendedores permitidos no ranking (ativo=True E ativo_ranking=True)
        vendedores_permitidos_ranking = set(
            Vendedor.objects.filter(
                ativo=True, 
                ativo_ranking=True
            ).values_list("nome_hardness", flat=True)
        )

        vendedores_ranking = [
            str(v).strip().upper() 
            for v in df['vendedor_nome'].dropna().unique()
            if str(v).strip().upper() not in ["", "NAN", "NONE"]
            and str(v).strip().upper() in vendedores_permitidos_ranking
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
            title=f"🏆 Ranking de Vendas - % da Meta Atingida ({data_inicio.strftime('%m/%Y')})",
            labels={'% Meta': '% da Meta', 'Nome_Display': 'Vendedora'},
            color='Cor',
            color_discrete_map='identity',
            text='% Meta'
        )

        fig_ranking.update_traces(texttemplate='%{text:.1f}%', textposition='outside')
        fig_ranking.update_layout(
            xaxis_tickangle=-45,
            yaxis=dict(
                            ticksuffix="%",
                            range=[0, max(float(df_ranking_final['% Meta'].dropna().max()) * 1.15, 100.0)]  # <-- Define o limite do eixo Y aqui dentro
                        ),
            showlegend=False,
            height=450,
            margin=dict(t=50, b=100, l=20, r=20)
        )
        grafico_ranking_html = fig_ranking.to_html(full_html=False, include_plotlyjs=False)

    return metricas, grafico_evolucao_html, grafico_barras_qtd_html, grafico_ranking_html


FATURAMENTO_MINIMO_INATIVIDADE = 500.0


def gerar_analise_clientes(df, vendedora_selecionada="EMPRESA", filtros_curva=None, filtros_status=None):
    """Gera KPIs, gráficos de risco e a tabela analítica de clientes."""
    if df.empty or 'cliente_nome' not in df.columns:
        return {}, "", "", pd.DataFrame()

    df = df.copy()
    df['data_emissao'] = pd.to_datetime(df['data_emissao'])
    df['valor_total'] = pd.to_numeric(df['valor_total'], errors='coerce').fillna(0.0).astype(float)

    # 1. Histórico GLOBAL de cada cliente ordenado por data crescente
    df_ordenado_global = df.sort_values('data_emissao')

    def get_ultimo_vendedor(serie):
        """Retorna o vendedor da última venda, ignorando valores nulos ou vazios."""
        validos = serie.dropna()
        validos = validos[~validos.astype(str).str.strip().str.upper().isin(['', 'NAN', 'NONE', 'DESCONHECIDO'])]
        return str(validos.iloc[-1]).strip().upper() if not validos.empty else '-'

    df_global_datas = df_ordenado_global.groupby('cliente_nome').agg(
        Ultima_Venda=('data_emissao', 'max'),
        Primeira_Venda=('data_emissao', 'min'),
        Vendedora=('vendedor_nome', get_ultimo_vendedor)  # <-- Vendedor da última venda realizada
    ).reset_index()

    # Cálculo do Faturamento Médio Mensal dos últimos 6 meses (180 dias)
    data_maxima_base = df['data_emissao'].max()
    seis_meses_antes = data_maxima_base - pd.Timedelta(days=180)
    df_ultimos_6m = df[df['data_emissao'] >= seis_meses_antes]
    ticket_6m_map = (df_ultimos_6m.groupby('cliente_nome')['valor_total'].sum() / 6.0).to_dict()

    # 2. Filtragem da Visão (EMPRESA ou Vendedor Específico)
    if vendedora_selecionada and vendedora_selecionada != "EMPRESA":
        df_visao = df[df['vendedor_nome'] == vendedora_selecionada].copy()
    else:
        df_visao = df.copy()

    if df_visao.empty:
        return {}, "", "", pd.DataFrame()

    # 3. Consolidação por Cliente (Totais da visão + Vendedor da última venda)
    df_clientes = df_visao.groupby('cliente_nome').agg(
        Faturamento_Total=('valor_total', 'sum'),
        Num_Vendas=('valor_total', 'count')
    ).reset_index()

    # Junta com os dados globais (trazendo a última data e o vendedor da última venda)
    df_clientes = pd.merge(df_clientes, df_global_datas, on='cliente_nome', how='left')

    # Filtro de faturamento mínimo
    df_clientes = df_clientes[df_clientes['Faturamento_Total'] >= FATURAMENTO_MINIMO_INATIVIDADE].copy()
    if df_clientes.empty:
        return {}, "", "", pd.DataFrame()

    # Tipagem numérica estrita
    df_clientes['Dias_Inatividade'] = (data_maxima_base - df_clientes['Ultima_Venda']).dt.days.fillna(0).astype(int)
    df_clientes['Ticket_Medio_6m'] = df_clientes['cliente_nome'].map(ticket_6m_map).fillna(0.0).astype(float)
    df_clientes['Faturamento_Total'] = df_clientes['Faturamento_Total'].astype(float)

    # Classificação de Curva (AA a D)[cite: 6]
    def classificar_curva(ticket):
        if ticket >= 5000: return 'AA'
        elif ticket >= 3000: return 'A'
        elif ticket >= 1500: return 'B'
        elif ticket >= 700: return 'C'
        return 'D'

    df_clientes['Curva'] = df_clientes['Ticket_Medio_6m'].apply(classificar_curva)

    # Classificação de Status[cite: 6]
    ano_vigente = data_maxima_base.year
    def definir_status(row):
        if row['Primeira_Venda'].year == ano_vigente:
            return 'Novo'
        elif row['Dias_Inatividade'] <= 30:
            return 'Ativo'
        elif row['Dias_Inatividade'] <= 90:
            return 'Em Risco'
        return 'Inativo'

    df_clientes['Status'] = df_clientes.apply(definir_status, axis=1)

    # 4. KPIs da Carteira
    kpis_carteira = {
        "total_clientes": len(df_clientes),
        "clientes_ativos": len(df_clientes[df_clientes['Status'] == 'Ativo']),
        "clientes_risco": len(df_clientes[df_clientes['Status'] == 'Em Risco']),
        "clientes_inativos": len(df_clientes[df_clientes['Status'] == 'Inativo']),
        "clientes_novos": len(df_clientes[df_clientes['Status'] == 'Novo']),
        "prioritarios": len(df_clientes[df_clientes['Curva'].isin(['AA', 'A', 'B'])]),
        "faturamento_carteira": f"R$ {df_clientes['Faturamento_Total'].sum():,.2f}"
    }

    # 5. Gráficos de Prioridade (Curvas AA, A e B)[cite: 6]
    grafico_status_html = ""
    grafico_matriz_html = ""
    df_prioridade = df_clientes[df_clientes['Curva'].isin(['AA', 'A', 'B'])].copy()

    if not df_prioridade.empty:
        # Gráfico 1: Status por Curva[cite: 6]
        fig_status = px.histogram(
            df_prioridade,
            x='Status',
            color='Curva',
            title="🎯 Clientes Alta Prioridade (AA, A, B) por Status",
            category_orders={"Status": ["Novo", "Ativo", "Em Risco", "Inativo"], "Curva": ["AA", "A", "B"]},
            color_discrete_map={"AA": "#00441b", "A": "#238b45", "B": "#74c476"},
            barmode="group",
            text_auto=True
        )
        fig_status.update_layout(yaxis_title="Qtd Clientes", height=380, margin=dict(t=40, b=20, l=20, r=20))
        grafico_status_html = fig_status.to_html(full_html=False, include_plotlyjs=False)

        # Gráfico 2: Matriz de Risco (Inatividade vs Ticket 6m)[cite: 6]
        # Cria uma coluna de tamanho segura para evitar quebra no Plotly
        df_prioridade['Tamanho_Bolha'] = df_prioridade['Faturamento_Total'].clip(lower=100.0)
        print(df_prioridade.head())
        
        fig_scatter = px.scatter(
            df_prioridade,
            x='Dias_Inatividade',
            y='Ticket_Medio_6m',
            color='Status',
            size='Tamanho_Bolha',
            size_max=28,
            hover_name='cliente_nome',
            hover_data={
                'Tamanho_Bolha': False,
                'Faturamento_Total': ':.2f',
                'Dias_Inatividade': True,
                'Ticket_Medio_6m': ':.2f'
            },
            title="⚠️ Matriz de Risco: Inatividade vs Fat. Médio Mensal",
            labels={'Dias_Inatividade': 'Dias sem comprar', 'Ticket_Medio_6m': 'Fat. Médio Mensal (6m)'},
            category_orders={"Status": ["Novo", "Ativo", "Em Risco", "Inativo"]},
            color_discrete_map={"Novo": "#17becf", "Ativo": "#2ca02c", "Em Risco": "#ff7f0e", "Inativo": "#d62728"}
        )
        # Se for Plotly:
        fig_scatter.update_traces(marker=dict(sizemode='area', sizeref=2.*max(df_prioridade['Tamanho_Bolha'])/(40.**2), sizemin=4))
        fig_scatter.add_vline(x=30, line_dash="dash", line_color="green", annotation_text="Ativos (30d)")
        fig_scatter.add_vline(x=90, line_dash="dash", line_color="red", annotation_text="Inativos (90d)")
        fig_scatter.update_layout(height=380, margin=dict(t=40, b=20, l=20, r=20))
        grafico_matriz_html = fig_scatter.to_html(full_html=False, include_plotlyjs=False)

    # 6. Preparação da Tabela Analítica[cite: 6]
    df_tabela = df_clientes.copy()
    if filtros_curva:
        df_tabela = df_tabela[df_tabela['Curva'].isin(filtros_curva)]
    if filtros_status:
        df_tabela = df_tabela[df_tabela['Status'].isin(filtros_status)]

    # Formata a data como string pronta para a tabela
    df_tabela['Ultima_Venda_str'] = df_tabela['Ultima_Venda'].dt.strftime('%d/%m/%Y')

    # Ordenação por Curva e Inatividade[cite: 6]
    ordem_map = {'AA': 1, 'A': 2, 'B': 3, 'C': 4, 'D': 5}
    df_tabela['ordem_curva'] = df_tabela['Curva'].map(ordem_map)
    df_tabela = df_tabela.sort_values(by=['ordem_curva', 'Dias_Inatividade'], ascending=[True, False]).drop(columns=['ordem_curva'])

    return kpis_carteira, grafico_status_html, grafico_matriz_html, df_tabela