# sales/views.py
from django.shortcuts import render, redirect
from django.utils import timezone
import pandas as pd
from django.contrib.auth.decorators import login_required
from .models import NotaFiscal, Vendedor
from .dashboard_services import gerar_metricas_e_graficos, gerar_analise_clientes


@login_required
def dashboard_vendas(request):
    user = request.user
    if getattr(user, "role", None) in ("ALMOXARIFADO", "COMPRAS") and not user.is_superuser:
        return redirect("dashboard_estoque")
    hoje = timezone.now().date()

    try:
        mes_selecionado = int(request.GET.get("mes", hoje.month))
    except ValueError:
        mes_selecionado = hoje.month
    try:
        ano_selecionado = int(request.GET.get("ano", hoje.year))
    except ValueError:
        ano_selecionado = hoje.year

    # 1. Carrega todas as notas faturadas no pandas (incluindo cliente_documento para consolidação por CNPJ)
    qs = NotaFiscal.objects.exclude(status="CANCELADA").values(
        "numero_nota",
        "cliente_nome",
        "cliente_documento",
        "data_emissao",
        "valor_total",
        "vendedor_nome",
    )
    df = pd.DataFrame(list(qs))

    vendedores_ativos = set(
        Vendedor.objects.filter(ativo=True).values_list("nome_hardness", flat=True)
    )

    # 2. Monta a lista filtrando pelo banco e removendo vazios / NAN
    vendedores_disponiveis = []
    if not df.empty and "vendedor_nome" in df.columns:
        nomes_na_base = [
            str(v).strip().upper()
            for v in df["vendedor_nome"].dropna().unique()
            if str(v).strip().upper() not in ["", "NAN", "NONE"]
        ]
        vendedores_disponiveis = sorted([v for v in nomes_na_base if v in vendedores_ativos])

    # 3. REGRA DE PERFIL
    if user.is_vendedor:
        vendedora_selecionada = (user.nome_vendedor_erp or user.first_name or user.username).strip().upper()
        pode_selecionar = False
    else:
        vendedora_selecionada = request.GET.get("visao", "EMPRESA").strip().upper()
        pode_selecionar = True

    pode_ver_margem = bool(getattr(user, "is_admin", False))

    # 4. Gera métricas e gráficos Plotly
    (
        metricas,
        graf_evolucao,
        graf_qtd,
        graf_ranking,
        graf_historico_metas,
        graf_top_clientes,
        tabela_notas,
    ) = gerar_metricas_e_graficos(
        df=df,
        vendedora_selecionada=vendedora_selecionada,
        mes_selecionado=mes_selecionado,
        ano_selecionado=ano_selecionado,
        incluir_margem_admin=pode_ver_margem,
    )

    context = {
        "vendedora_selecionada": vendedora_selecionada,
        "vendedores_disponiveis": vendedores_disponiveis,
        "pode_selecionar": pode_selecionar,
        "pode_ver_margem": pode_ver_margem,
        "mes_selecionado": mes_selecionado,
        "ano_selecionado": ano_selecionado,
        "metricas": metricas,
        "grafico_evolucao": graf_evolucao,
        "grafico_qtd": graf_qtd,
        "grafico_ranking": graf_ranking,
        "grafico_historico_metas": graf_historico_metas,
        "grafico_top_clientes": graf_top_clientes,
        "tabela_notas": tabela_notas,
        "meses": [
            (1, "Jan"),
            (2, "Fev"),
            (3, "Mar"),
            (4, "Abr"),
            (5, "Mai"),
            (6, "Jun"),
            (7, "Jul"),
            (8, "Ago"),
            (9, "Set"),
            (10, "Out"),
            (11, "Nov"),
            (12, "Dez"),
        ],
        "anos": [hoje.year, hoje.year - 1, hoje.year - 2],
    }
    return render(request, "sales/dashboard.html", context)


@login_required
def analise_clientes_view(request):
    user = request.user
    if getattr(user, "role", None) in ("ALMOXARIFADO", "COMPRAS") and not user.is_superuser:
        return redirect("dashboard_estoque")

    # 1. Regra de Perfil: Vendedor só pode visualizar sua própria carteira atual
    if user.is_vendedor:
        visao_selecionada = (user.nome_vendedor_erp or user.first_name or user.username).strip().upper()
        pode_selecionar = False
    else:
        visao_selecionada = request.GET.get("visao", "EMPRESA").strip().upper()
        pode_selecionar = True

    # 2. Carrega todas as notas válidas do banco (incluindo cliente_documento para consolidação por CNPJ)
    notas_qs = NotaFiscal.objects.exclude(status="CANCELADA").values(
        "cliente_nome",
        "cliente_documento",
        "vendedor_nome",
        "data_emissao",
        "valor_total",
    )
    df = pd.DataFrame(list(notas_qs))

    # 3. Monta a lista de vendedores ativos para o seletor
    vendedores_qs = (
        Vendedor.objects.filter(ativo=True)
        .exclude(nome_hardness__in=["", "NAN", "NONE", "DESCONHECIDO"])
        .values_list("nome_hardness", flat=True)
        .order_by("nome_hardness")
    )

    vendedores_disponiveis = list(vendedores_qs)

    if not vendedores_disponiveis and not df.empty and "vendedor_nome" in df.columns:
        nomes_unicos = [
            str(v).strip().upper()
            for v in df["vendedor_nome"].dropna().unique()
            if str(v).strip().upper() not in ["", "NAN", "NONE", "DESCONHECIDO"]
        ]
        vendedores_disponiveis = sorted(nomes_unicos)

    # 4. Captura filtros opcionais de Curva e Status
    curvas_selecionadas = request.GET.getlist("curva") or ["AA", "A", "B"]
    status_selecionados = request.GET.getlist("status") or ["Novo", "Ativo", "Em Risco", "Inativo"]

    # 5. Executa os cálculos e gera os gráficos
    kpis, graf_status, graf_matriz, df_tabela = gerar_analise_clientes(
        df=df,
        vendedora_selecionada=visao_selecionada,
        filtros_curva=curvas_selecionadas,
        filtros_status=status_selecionados,
    )

    tabela_linhas = df_tabela.to_dict(orient="records") if not df_tabela.empty else []

    context = {
        "vendedores_disponiveis": vendedores_disponiveis,
        "visao_selecionada": visao_selecionada,
        "pode_selecionar": pode_selecionar,
        "kpis": kpis,
        "grafico_status": graf_status,
        "grafico_matriz": graf_matriz,
        "tabela_clientes": tabela_linhas,
        "curvas_disponiveis": ["AA", "A", "B", "C", "D"],
        "status_disponiveis": ["Novo", "Ativo", "Em Risco", "Inativo"],
        "curvas_selecionadas": curvas_selecionadas,
        "status_selecionados": status_selecionados,
    }
    return render(request, "sales/analise_clientes.html", context)