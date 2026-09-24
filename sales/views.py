# sales/views.py
from datetime import datetime
from django.shortcuts import render
from django.utils import timezone
import pandas as pd
from accounts.decorators import roles_required
from django.contrib.auth.decorators import login_required
from .models import NotaFiscal, Vendedor
from .dashboard_services import gerar_metricas_e_graficos
from .dashboard_services import gerar_analise_clientes

@roles_required('ADMINISTRADOR', 'SUPERVISOR', 'VENDEDOR')
def dashboard_vendas(request):
    user = request.user
    hoje = timezone.now().date()

    mes_selecionado = int(request.GET.get('mes', hoje.month))
    ano_selecionado = int(request.GET.get('ano', hoje.year))

    # 1. Carrega todas as notas faturadas no pandas
    qs = NotaFiscal.objects.exclude(status="CANCELADA").values(
        'numero_nota', 'cliente_nome', 'data_emissao', 'valor_total', 'vendedor_nome'
    )
    df = pd.DataFrame(list(qs))

    vendedores_ativos = set(
        Vendedor.objects.filter(ativo=True).values_list("nome_hardness", flat=True)
    )

    # 2. Monta a lista filtrando pelo banco e removendo vazios / NAN
    vendedores_disponiveis = []
    if not df.empty and 'vendedor_nome' in df.columns:
        nomes_na_base = [
            str(v).strip().upper() 
            for v in df['vendedor_nome'].dropna().unique() 
            if str(v).strip().upper() not in ["", "NAN", "NONE"]
        ]
        # Mantém apenas quem tem ativo=True no model Vendedor
        vendedores_disponiveis = sorted([v for v in nomes_na_base if v in vendedores_ativos])
        
    # 3. REGRA DE PERFIL
    if user.is_vendedor:
        # Vendedor só acessa o seu próprio nome cadastrado
        vendedora_selecionada = (user.nome_vendedor_erp or user.first_name or user.username).upper()
        pode_selecionar = False
    else:
        # Administrador e Supervisor podem escolher
        vendedora_selecionada = request.GET.get('visao', 'EMPRESA')
        pode_selecionar = True

    # 4. Gera métricas e gráficos Plotly
    metricas, fig_evolucao, fig_barras, fig_ranking = gerar_metricas_e_graficos(
        df, vendedora_selecionada, mes_selecionado, ano_selecionado
    )

    context = {
        "vendedora_selecionada": vendedora_selecionada,
        "vendedores_disponiveis": vendedores_disponiveis,
        "pode_selecionar": pode_selecionar,
        "mes_selecionado": mes_selecionado,
        "ano_selecionado": ano_selecionado,
        "metricas": metricas,
        "fig_evolucao": fig_evolucao,
        "fig_barras": fig_barras,
        "fig_ranking": fig_ranking,
        "meses": [(1, "Jan"), (2, "Fev"), (3, "Mar"), (4, "Abr"), (5, "Mai"), (6, "Jun"),
                  (7, "Jul"), (8, "Ago"), (9, "Set"), (10, "Out"), (11, "Nov"), (12, "Dez")],
        "anos": [hoje.year, hoje.year - 1]
    }
    return render(request, "sales/dashboard.html", context)

@login_required
def analise_clientes_view(request):
    # 1. Pega a visão selecionada (Padrão: EMPRESA)
    visao_selecionada = request.GET.get('visao', 'EMPRESA').strip().upper()

    # 2. Carrega todas as notas válidas do banco
    notas_qs = NotaFiscal.objects.exclude(status='CANCELADA').values(
        'cliente_nome', 'vendedor_nome', 'data_emissao', 'valor_total'
    )
    df = pd.DataFrame(list(notas_qs))

    # 3. Monta a lista de vendedores ativos para o seletor
    vendedores_qs = Vendedor.objects.filter(ativo=True).exclude(
        nome_hardness__in=["", "NAN", "NONE", "DESCONHECIDO"]
    ).values_list("nome_hardness", flat=True).order_by("nome_hardness")
    
    vendedores_disponiveis = list(vendedores_qs)
    
    # Fallback caso a tabela Vendedor ainda não tenha sido populada
    if not vendedores_disponiveis and not df.empty and 'vendedor_nome' in df.columns:
        nomes_unicos = [
            str(v).strip().upper() for v in df['vendedor_nome'].dropna().unique()
            if str(v).strip().upper() not in ["", "NAN", "NONE", "DESCONHECIDO"]
        ]
        vendedores_disponiveis = sorted(nomes_unicos)

    # 4. Captura filtros opcionais de Curva e Status
    curvas_selecionadas = request.GET.getlist('curva')
    status_selecionados = request.GET.getlist('status')

    # 5. Executa os cálculos e gera os gráficos
    kpis, graf_status, graf_matriz, df_tabela = gerar_analise_clientes(
        df=df,
        vendedora_selecionada=visao_selecionada,
        filtros_curva=curvas_selecionadas,
        filtros_status=status_selecionados
    )

    tabela_linhas = df_tabela.to_dict(orient='records') if not df_tabela.empty else []

    context = {
        'vendedores_disponiveis': vendedores_disponiveis,
        'visao_selecionada': visao_selecionada,
        'kpis': kpis,
        'grafico_status': graf_status,
        'grafico_matriz': graf_matriz,
        'tabela_clientes': tabela_linhas,
        'curvas_disponiveis': ['AA', 'A', 'B', 'C', 'D'],
        'status_disponiveis': ['Novo', 'Ativo', 'Em Risco', 'Inativo'],
        'curvas_selecionadas': curvas_selecionadas,
        'status_selecionados': status_selecionados,
    }
    return render(request, 'sales/analise_clientes.html', context)