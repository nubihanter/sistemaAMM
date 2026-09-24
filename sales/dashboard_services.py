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


def obter_historico_metas_vendedor(vendedora_selecionada, mes_selecionado, ano_selecionado, limite=3):
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
        m for m in todas_metas
        if normalizar_nome(m.vendedor_nome) == nome_piperun_alvo
        and (m.ano < ano_selecionado or (m.ano == ano_selecionado and m.mes <= mes_selecionado))
    ]

    # Ordena decrescente para pegar as N mais recentes e reordena cronologicamente
    metas_vendedor.sort(key=lambda m: (m.ano, m.mes), reverse=True)
    ultimas_metas = metas_vendedor[:limite]
    ultimas_metas.sort(key=lambda m: (m.ano, m.mes))
    return ultimas_metas


def gerar_metricas_e_graficos(df, vendedora_selecionada, mes_selecionado, ano_selecionado):
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
        "dias_info": ""
    }

    if df.empty:
        return metricas_vazias, "", "", "", ""

    df['data_emissao'] = pd.to_datetime(df['data_emissao'])
    df['valor_total'] = pd.to_numeric(df['valor_total'], errors='coerce').fillna(0)

    # Período do filtro
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

    # --- CÁLCULO PONDERADO POR DIAS DECORRIDOS (LÓGICA DE RITMO) ---
    hoje = datetime.now().date()
    dias_no_mes = (data_fim - data_inicio).days + 1

    if ano_selecionado < hoje.year or (ano_selecionado == hoje.year and mes_selecionado < hoje.month):
        dias_decorridos = dias_no_mes
    elif ano_selecionado > hoje.year or (ano_selecionado == hoje.year and mes_selecionado > hoje.month):
        dias_decorridos = 0
    else:
        dias_decorridos = min(hoje.day, dias_no_mes)

    proporcao_decorrida = (dias_decorridos / dias_no_mes) if dias_no_mes > 0 else 1.0
    meta_proporcional = meta_periodo * proporcao_decorrida

    percentual_ritmo = (total_vendas / meta_proporcional * 100) if meta_proporcional > 0 else (100.0 if percentual_meta >= 100 else 0.0)

    # Avaliação de status ponderada pelo ritmo
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

    delta_ritmo = f"{percentual_ritmo:.1f}% do ritmo esperado" if (proporcao_decorrida < 1.0 and dias_decorridos > 0) else ""
    dias_info = f"Dia {dias_decorridos}/{dias_no_mes} (Meta proporcional: R$ {meta_proporcional:,.2f})"

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
        "dias_info": dias_info
    }

    # Gráfico 1: Evolução Diária Acumulada
    grafico_evolucao_html = ""
    grafico_barras_qtd_html = ""
    if not df_filtered.empty:
        df_filtered['Data'] = df_filtered['data_emissao'].dt.date
        df_diario = df_filtered.groupby('Data').agg({'valor_total': ['sum', 'count']}).reset_index()
        df_diario.columns = ['Data', 'Valor', 'Quantidade']
        df_diario = df_diario.sort_values('Data')
        df_diario["Valor Acumulado"] = df_diario["Valor"].cumsum()

        fig_linha = px.line(df_diario, x='Data', y='Valor Acumulado', markers=True, title="Evolução Diária Acumulada (R$)")
        if meta_periodo > 0:
            fig_linha.add_hline(y=meta_periodo, line_dash="dash", line_color="green", annotation_text="Meta Total", annotation_position="top left")
        fig_linha.update_layout(height=350, margin=dict(t=40, b=20, l=20, r=20))
        grafico_evolucao_html = fig_linha.to_html(full_html=False, include_plotlyjs=False)

        fig_barras = px.bar(df_diario, x='Data', y='Quantidade', title="Nº Vendas por Dia")
        fig_barras.update_layout(height=350, margin=dict(t=40, b=20, l=20, r=20))
        grafico_barras_qtd_html = fig_barras.to_html(full_html=False, include_plotlyjs=False)

    # Gráfico 2: Ranking por % da Meta
    grafico_ranking_html = ""
    df_periodo_geral = df[
        (df['data_emissao'].dt.date >= data_inicio.date()) &
        (df['data_emissao'].dt.date <= data_fim.date())
    ].copy()

    ranking_data = []
    if 'vendedor_nome' in df.columns:
        vendedores_permitidos_ranking = set(
            Vendedor.objects.filter(ativo=True, ativo_ranking=True).values_list("nome_hardness", flat=True)
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
                "Vendedor": vend,
                "% Meta": pct_meta,
                "Total_Vendas": total_vend,
                "Meta": meta_vend,
                "tem_meta": meta_vend > 0
            })

    if ranking_data:
        df_rank = pd.DataFrame(ranking_data)
        df_com_meta = df_rank[df_rank['tem_meta']].copy().sort_values('% Meta', ascending=False).reset_index(drop=True)
        df_sem_meta = df_rank[~df_rank['tem_meta']].copy().sort_values('Vendedor', ascending=True).reset_index(drop=True)

        df_com_meta['Posição'] = range(1, len(df_com_meta) + 1)
        df_sem_meta['Posição'] = range(len(df_com_meta) + 1, len(df_com_meta) + len(df_sem_meta) + 1)

        df_ranking_final = pd.concat([df_com_meta, df_sem_meta], ignore_index=True)

        def formata_nome(row):
            pos = int(row['Posição'])
            nome = row['Vendedor']
            if pos == 1: return f"🥇 {nome}"
            elif pos == 2: return f"🥈 {nome}"
            elif pos == 3: return f"🥉 {nome}"
            return f"{pos}º {nome}"

        def define_cor(pos):
            if pos == 1: return "#ffd700"
            elif pos == 2: return "#c0c0c0"
            elif pos == 3: return "#cd7f32"
            return "#1f77b4"

        df_ranking_final['Nome_Display'] = df_ranking_final.apply(formata_nome, axis=1)
        df_ranking_final['Cor'] = df_ranking_final['Posição'].apply(define_cor)

        fig_ranking = px.bar(
            df_ranking_final,
            x='Nome_Display',
            y='% Meta',
            title=f"🏆 Ranking de Vendas - % da Meta Atingida ({data_inicio.strftime('%m/%Y')})",
            labels={'% Meta': '% da Meta', 'Nome_Display': 'Vendedor'},
            color='Cor',
            color_discrete_map='identity',
            text='% Meta'
        )
        fig_ranking.update_traces(texttemplate='%{text:.1f}%', textposition='outside')
        fig_ranking.update_layout(
            xaxis_tickangle=-45,
            yaxis=dict(
                ticksuffix="%",
                range=[0, max(float(df_ranking_final['% Meta'].dropna().max()) * 1.15, 100.0)]
            ),
            showlegend=False,
            height=450,
            margin=dict(t=50, b=100, l=20, r=20)
        )
        grafico_ranking_html = fig_ranking.to_html(full_html=False, include_plotlyjs=False)

    # Gráfico 3: Histórico de Metas vs Realizado (Últimas 3 Metas)
    grafico_historico_metas_html = ""
    ultimas_metas_banco = obter_historico_metas_vendedor(vendedora_selecionada, mes_selecionado, ano_selecionado, limite=3)
    historico_plot_data = []

    for m in ultimas_metas_banco:
        dt_m_ini = pd.Timestamp(year=m.ano, month=m.mes, day=1)
        if m.mes == 12:
            dt_m_fim = pd.Timestamp(year=m.ano + 1, month=1, day=1) - timedelta(days=1)
        else:
            dt_m_fim = pd.Timestamp(year=m.ano, month=m.mes + 1, day=1) - timedelta(days=1)

        vendas_m = df_vendedor[
            (df_vendedor['data_emissao'].dt.date >= dt_m_ini.date()) &
            (df_vendedor['data_emissao'].dt.date <= dt_m_fim.date())
        ]['valor_total'].sum()

        periodo_label = f"{m.mes:02d}/{m.ano}"
        historico_plot_data.append({'Período': periodo_label, 'Tipo': 'Meta', 'Valor': float(m.valor)})
        historico_plot_data.append({'Período': periodo_label, 'Tipo': 'Realizado', 'Valor': float(vendas_m)})

    if not historico_plot_data and meta_periodo > 0:
        periodo_label = f"{mes_selecionado:02d}/{ano_selecionado}"
        historico_plot_data.append({'Período': periodo_label, 'Tipo': 'Meta', 'Valor': float(meta_periodo)})
        historico_plot_data.append({'Período': periodo_label, 'Tipo': 'Realizado', 'Valor': float(total_vendas)})

    if historico_plot_data:
        df_compare = pd.DataFrame(historico_plot_data)
        fig_compare = px.bar(
            df_compare,
            x='Período',
            y='Valor',
            color='Tipo',
            barmode='group',
            title="Últimas Metas vs Realizado",
            labels={'Valor': 'Valor (R$)', 'Período': 'Mês/Ano'},
            color_discrete_map={'Meta': '#1f77b4', 'Realizado': '#ff7f0e'},
            text_auto=True
        )
        fig_compare.update_layout(height=350, margin=dict(t=40, b=20, l=20, r=20))
        grafico_historico_metas_html = fig_compare.to_html(full_html=False, include_plotlyjs=False)

    return metricas, grafico_evolucao_html, grafico_barras_qtd_html, grafico_ranking_html, grafico_historico_metas_html


FATURAMENTO_MINIMO_INATIVIDADE = 500.0


def calcular_ticket_medio_6m_clientes(df):
    """
    Calcula o faturamento médio mensal retroativo à ÚLTIMA VENDA de cada cliente.
    Se o cliente tiver histórico menor que 6 meses, divide apenas pelos meses decorridos.
    """
    ticket_map = {}
    df_ordenado = df.sort_values('data_emissao')

    for cliente, grupo in df_ordenado.groupby('cliente_nome'):
        ult_data = grupo['data_emissao'].max()
        prim_data = grupo['data_emissao'].min()

        # Janela de 6 meses retroativos à última compra do cliente
        janela_inicio = ult_data - pd.Timedelta(days=180)
        vendas_janela = grupo[grupo['data_emissao'] >= janela_inicio]['valor_total'].sum()

        # Se o cliente já comprava antes da janela de 180 dias, divide por 6 meses
        if prim_data <= janela_inicio:
            meses_divisor = 6.0
        else:
            # Calcula a quantidade de meses entre a primeira e a última venda
            meses_decorridos = (ult_data.year - prim_data.year) * 12 + (ult_data.month - prim_data.month) + 1
            meses_divisor = min(6.0, max(1.0, float(meses_decorridos)))

        ticket_map[cliente] = float(vendas_janela) / meses_divisor

    return ticket_map


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
        Vendedora=('vendedor_nome', get_ultimo_vendedor)  # Vendedor da última venda
    ).reset_index()

    # 2. Cálculo do Ticket Médio 6m Individual por Cliente
    ticket_6m_map = calcular_ticket_medio_6m_clientes(df)

    # 3. Filtragem da Visão (EMPRESA = Todos; ou Vendedor Específica)
    if vendedora_selecionada and vendedora_selecionada != "EMPRESA":
        df_visao = df[df['vendedor_nome'] == vendedora_selecionada].copy()
    else:
        df_visao = df.copy()

    if df_visao.empty:
        return {}, "", "", pd.DataFrame()

    # 4. Consolidação por Cliente na visão selecionada
    df_clientes = df_visao.groupby('cliente_nome').agg(
        Faturamento_Total=('valor_total', 'sum'),
        Num_Vendas=('valor_total', 'count')
    ).reset_index()

    # Une totais da visão com os metadados globais do cliente
    df_clientes = pd.merge(df_clientes, df_global_datas, on='cliente_nome', how='left')

    # Filtro de faturamento mínimo acumulado
    df_clientes = df_clientes[df_clientes['Faturamento_Total'] >= FATURAMENTO_MINIMO_INATIVIDADE].copy()
    if df_clientes.empty:
        return {}, "", "", pd.DataFrame()

    data_maxima_base = df['data_emissao'].max()
    ano_vigente = data_maxima_base.year

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
    def definir_status(row):
        if row['Primeira_Venda'].year == ano_vigente:
            return 'Novo'
        elif row['Dias_Inatividade'] <= 30:
            return 'Ativo'
        elif row['Dias_Inatividade'] <= 90:
            return 'Em Risco'
        return 'Inativo'

    df_clientes['Status'] = df_clientes.apply(definir_status, axis=1)

    curvas_aplicadas = filtros_curva or ['AA', 'A', 'B']
    status_aplicados = filtros_status or ['Novo', 'Ativo', 'Em Risco', 'Inativo']
    df_filtrado = df_clientes[
        df_clientes['Curva'].isin(curvas_aplicadas) &
        df_clientes['Status'].isin(status_aplicados)
    ].copy()

    # 5. KPIs da Carteira
    kpis_carteira = {
        "total_clientes": len(df_clientes),
        "clientes_ativos": len(df_clientes[df_clientes['Status'] == 'Ativo']),
        "clientes_risco": len(df_clientes[df_clientes['Status'] == 'Em Risco']),
        "clientes_inativos": len(df_clientes[df_clientes['Status'] == 'Inativo']),
        "clientes_novos": len(df_clientes[df_clientes['Status'] == 'Novo']),
        "prioritarios": len(df_clientes[df_clientes['Curva'].isin(['AA', 'A', 'B'])]),
        "faturamento_carteira": f"R$ {df_clientes['Faturamento_Total'].sum():,.2f}"
    }

    # 6. Gráficos de Prioridade (Curvas AA, A e B)[cite: 6]
    grafico_status_html = ""
    grafico_matriz_html = ""
    df_prioridade = df_filtrado

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
        df_prioridade['Tamanho_Bolha'] = df_prioridade['Faturamento_Total'].clip(lower=100.0)

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
        fig_scatter.update_traces(
            marker=dict(
                sizemode='area',
                sizeref=2. * max(df_prioridade['Tamanho_Bolha']) / (40. ** 2),
                sizemin=4
            )
        )
        fig_scatter.add_vline(x=30, line_dash="dash", line_color="green", annotation_text="Ativos", annotation_position="top left")
        fig_scatter.add_vline(x=90, line_dash="dash", line_color="red", annotation_text="Inativos", annotation_position="top right")
        fig_scatter.update_layout(height=380, margin=dict(t=40, b=20, l=20, r=20))
        grafico_matriz_html = fig_scatter.to_html(full_html=False, include_plotlyjs=True)

    # 7. Preparação da Tabela Analítica[cite: 6]
    df_tabela = df_clientes.copy()
    df_tabela = df_tabela[
        df_tabela['Curva'].isin(curvas_aplicadas) &
        df_tabela['Status'].isin(status_aplicados)
    ]

    df_tabela['Ultima_Venda_str'] = df_tabela['Ultima_Venda'].dt.strftime('%d/%m/%Y')

    # Ordenação por Curva e Inatividade[cite: 6]
    ordem_map = {'AA': 1, 'A': 2, 'B': 3, 'C': 4, 'D': 5}
    df_tabela['ordem_curva'] = df_tabela['Curva'].map(ordem_map)
    df_tabela = df_tabela.sort_values(by=['ordem_curva', 'Dias_Inatividade'], ascending=[True, False]).drop(columns=['ordem_curva'])

    return kpis_carteira, grafico_status_html, grafico_matriz_html, df_tabela