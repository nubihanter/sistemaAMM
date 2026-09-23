# sales/views.py
from datetime import datetime
from django.shortcuts import render
from django.utils import timezone
import pandas as pd
from accounts.decorators import roles_required
from .models import NotaFiscal
from .dashboard_services import gerar_metricas_e_graficos, VENDEDORES_OCULTOS

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

    # 2. Lista de vendedores elegíveis
    vendedores_disponiveis = []
    if not df.empty and 'vendedor_nome' in df.columns:
        vendedores_disponiveis = sorted([
            str(v).strip() 
            for v in df['vendedor_nome'].dropna().unique() 
            if str(v).strip() and str(v).strip() != "NAN"
        ])
        
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