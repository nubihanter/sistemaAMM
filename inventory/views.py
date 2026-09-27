import csv
from datetime import datetime, timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from integrations.ca_epi import ConsultaCAClient
from .models import Categoria, CertificadoAprovacao, ProdutoEPI
from .dashboard_services import gerar_analise_estoque_e_compras
from .services import sincronizar_estoque_e_itens_rapido, sincronizar_vencimentos_ca


def _usuario_pode_gerenciar_produtos(user) -> bool:
    """Apenas perfis COMPRAS, ADMINISTRADOR e ALMOXARIFADO possuem acesso à gestão e sincronização de estoque."""
    return bool(
        getattr(user, "is_admin", False)
        or getattr(user, "is_compras", False)
        or getattr(user, "is_almoxarifado", False)
    )


def _obter_redirect_seguro(request, fallback: str) -> str:
    """Garante que o redirecionamento pós-POST ocorra apenas para hosts permitidos."""
    candidato = request.POST.get("next") or request.META.get("HTTP_REFERER") or ""
    if candidato and url_has_allowed_host_and_scheme(
        url=candidato,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidato
    return fallback


@login_required
def dashboard_estoque_view(request):
    try:
        dias_analise = int(request.GET.get("dias", 90))
    except ValueError:
        dias_analise = 90

    try:
        cobertura_meses = float(request.GET.get("cobertura", 2.0))
    except ValueError:
        cobertura_meses = 2.0

    try:
        teto_meses = float(request.GET.get("teto", 4.0))
    except ValueError:
        teto_meses = 4.0

    cliente_selecionado = request.GET.get("cliente", "").strip()
    marca_selecionada = request.GET.get("marca", "TODAS").strip()
    status_filtro = request.GET.get("status", "TODOS").strip()
    apenas_criticos = request.GET.get("criticos", "") == "1"
    aba_ativa = request.GET.get("aba", "compras").strip()

    pode_ver_valores_financeiros = bool(getattr(request.user, "is_admin", False))

    dados = gerar_analise_estoque_e_compras(
        dias_analise=dias_analise,
        cobertura_meses=cobertura_meses,
        teto_meses=teto_meses,
        cliente_selecionado=cliente_selecionado,
        marca_selecionada=marca_selecionada,
        status_filtro=status_filtro,
        apenas_criticos=apenas_criticos,
    )

    if not pode_ver_valores_financeiros and dados.get("tabela_ranking"):
        dados["tabela_ranking"] = sorted(dados["tabela_ranking"], key=lambda x: x.get("Posicao_Qtd", 9999))

    context = {
        **dados,
        "pode_ver_valores_financeiros": pode_ver_valores_financeiros,
        "dias_analise": dias_analise,
        "cobertura_meses": int(cobertura_meses) if cobertura_meses.is_integer() else cobertura_meses,
        "teto_meses": int(teto_meses) if teto_meses.is_integer() else teto_meses,
        "marca_selecionada": marca_selecionada,
        "status_filtro": status_filtro,
        "apenas_criticos": apenas_criticos,
        "aba_ativa": aba_ativa,
        "opcoes_dias": [
            (30, "Últimos 30 dias"),
            (60, "Últimos 60 dias"),
            (90, "Últimos 90 dias (Padrão)"),
            (180, "Últimos 6 meses (180d)"),
            (365, "Último Ano (365d)"),
        ],
        "opcoes_cobertura": [1, 2, 3, 4, 6],
        "opcoes_teto": [3, 4, 6, 9, 12],
        "opcoes_status": [
            ("TODOS", "Todos os Status"),
            ("CRÍTICO / RUPTURA", "🔴 Crítico / Ruptura (Urgente)"),
            ("COMPRAR", "🟠 Comprar (Abaixo da Meta)"),
            ("ESPORÁDICO", "🟣 Item Esporádico (Avaliar)"),
            ("ACIMA DO NECESSÁRIO", "🔵 Acima do Necessário (Excesso)"),
            ("PARADO (SEM GIRO)", "⚫ Parado (Sem Giro no Período)"),
            ("SAUDÁVEL", "🟢 Estoque Saudável"),
        ],
    }
    return render(request, "inventory/dashboard_estoque.html", context)


@login_required
@require_POST
def atualizar_parametros_produto_view(request):
    """
    Permite estabelecer o Estoque Mínimo, Estoque Máximo e marcar/desmarcar
    o produto como Crítico (que não pode faltar) diretamente pelo dashboard.
    """
    if not _usuario_pode_gerenciar_produtos(request.user):
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(
                {"ok": False, "error": "Sem permissão para alterar parâmetros de estoque."},
                status=403,
            )
        messages.error(request, "Sem permissão para alterar parâmetros de estoque.")
        return redirect(_obter_redirect_seguro(request, "/inventory/dashboard/"))

    sku = request.POST.get("sku", "").strip()
    nome_fallback = request.POST.get("nome", sku).strip()
    marca_fallback = request.POST.get("marca", "N/A").strip()

    if not sku:
        return JsonResponse({"ok": False, "error": "SKU não informado."}, status=400)

    cat_geral, _ = Categoria.objects.get_or_create(nome="GERAL")
    produto, _ = ProdutoEPI.objects.get_or_create(
        sku=sku,
        defaults={
            "nome": nome_fallback or sku,
            "marca": marca_fallback,
            "categoria": cat_geral,
        },
    )

    acao = request.POST.get("acao", "salvar_modal")
    if acao == "toggle_critico":
        produto.item_critico = not produto.item_critico
        if produto.item_critico and produto.estoque_minimo == 0:
            try:
                sugestao_min = int(float(request.POST.get("sugestao_minimo", 0)))
            except ValueError:
                sugestao_min = 0
            if sugestao_min > 0:
                produto.estoque_minimo = sugestao_min
        produto.save(update_fields=["item_critico", "estoque_minimo", "data_atualizacao"])
    else:
        try:
            est_min = max(0, int(request.POST.get("estoque_minimo", 0)))
        except ValueError:
            est_min = produto.estoque_minimo

        try:
            est_max = max(0, int(request.POST.get("estoque_maximo", 0)))
        except ValueError:
            est_max = produto.estoque_maximo

        item_critico = request.POST.get("item_critico") in ("on", "true", "1", "True")

        produto.estoque_minimo = est_min
        produto.estoque_maximo = est_max
        produto.item_critico = item_critico
        produto.save(update_fields=["estoque_minimo", "estoque_maximo", "item_critico", "data_atualizacao"])

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "ok": True,
                "sku": produto.sku,
                "item_critico": produto.item_critico,
                "estoque_minimo": produto.estoque_minimo,
                "estoque_maximo": produto.estoque_maximo,
            }
        )

    messages.success(
        request,
        f"Parâmetros do produto {produto.sku} atualizados (Crítico: {'Sim' if produto.item_critico else 'Não'} | Mín: {produto.estoque_minimo} | Máx: {produto.estoque_maximo}).",
    )
    return redirect(_obter_redirect_seguro(request, "/inventory/dashboard/"))


@login_required
@require_POST
def sincronizar_estoque_manual_view(request):
    """Dispara a sincronização incremental de estoque e itens vendidos do Hardness."""
    if not _usuario_pode_gerenciar_produtos(request.user):
        messages.error(request, "Sem permissão para disparar sincronização de estoque.")
        return redirect(_obter_redirect_seguro(request, "/inventory/dashboard/"))

    from sales.services import registrar_execucao_sincronizacao

    try:
        _, res = registrar_execucao_sincronizacao(
            tipo="ESTOQUE_HARDNESS",
            funcao_sync=lambda: sincronizar_estoque_e_itens_rapido(dias_retroativos_padrao=60),
            origem="MANUAL_PAINEL",
            usuario=request.user.username,
        )
        messages.success(
            request,
            f"Sincronização concluída! Estoque: {res['estoque_criados']} novos / {res['estoque_atualizados']} atualizados | "
            f"Itens de Venda: {res['itens_criados']} novos / {res['itens_atualizados']} atualizados.",
        )
    except Exception as e:
        messages.error(request, f"Erro ao sincronizar com o Hardness: {e}")

    return redirect(_obter_redirect_seguro(request, "/inventory/dashboard/"))


@login_required
def exportar_compras_csv_view(request):
    """Exporta a tabela de projeção de compras e diagnóstico de estoque em Excel (.xlsx)."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    pode_ver_valores_financeiros = bool(getattr(request.user, "is_admin", False))

    try:
        dias_analise = int(request.GET.get("dias", 90))
    except ValueError:
        dias_analise = 90
    try:
        cobertura_meses = float(request.GET.get("cobertura", 2.0))
    except ValueError:
        cobertura_meses = 2.0
    try:
        teto_meses = float(request.GET.get("teto", 4.0))
    except ValueError:
        teto_meses = 4.0

    marca_selecionada = request.GET.get("marca", "TODAS").strip()
    status_filtro = request.GET.get("status", "TODOS").strip()
    apenas_criticos = request.GET.get("criticos", "") == "1"

    dados = gerar_analise_estoque_e_compras(
        dias_analise=dias_analise,
        cobertura_meses=cobertura_meses,
        teto_meses=teto_meses,
        marca_selecionada=marca_selecionada,
        status_filtro=status_filtro,
        apenas_criticos=apenas_criticos,
        gerar_graficos=False,
        incluir_aba_cliente=False,
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "Plano de Compras"

    cabecalho = [
        "Código (SKU)",
        "Descrição do Produto",
        "Marca",
        "Unidade",
        "CA",
        "Validade CA",
        "Dias p/ Vencer CA",
        "Item Crítico",
        "Curva ABC",
        "Padrão Demanda",
        f"Qtd Vendida ({dias_analise}d)",
        "Pedidos no Período",
        "Clientes Distintos",
        "Média Mensal (un)",
        "Tendência (un/mês)",
        "Estoque Atual",
        "Em Compra (OC)",
        "Estoque Físico",
        "Estoque Mínimo",
        f"Meta Linear ({cobertura_meses:g}M)",
        f"Teto Excesso ({teto_meses:g}M)",
        "Cobertura Atual (Meses)",
        "Status Estoque",
        "Sugestão Compra (un)",
    ]
    if pode_ver_valores_financeiros:
        cabecalho.extend(["Custo Unit. Ref (R$)", "Valor Estimado Compra (R$)"])
    cabecalho.extend(["Qtd Excesso (un)", "Última Venda"])

    ws.append(cabecalho)

    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=10)
    thin_border = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )

    for col_idx in range(1, len(cabecalho) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    status_fills = {
        "CRÍTICO / RUPTURA": PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
        "COMPRAR": PatternFill(start_color="FFEDD5", end_color="FFEDD5", fill_type="solid"),
        "ESPORÁDICO": PatternFill(start_color="EDE9FE", end_color="EDE9FE", fill_type="solid"),
        "ACIMA DO NECESSÁRIO": PatternFill(start_color="E0F2FE", end_color="E0F2FE", fill_type="solid"),
        "PARADO (SEM GIRO)": PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid"),
        "SAUDÁVEL": PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid"),
    }

    for row_idx, r in enumerate(dados["tabela_compras"], start=2):
        dias_ca_val = int(r["Dias_Para_Vencer_CA"]) if r.get("Tem_Validade_CA") else "-"
        linha = [
            str(r["codigo_produto"]),
            r["nome"],
            r["marca"],
            r["unidade_medida"],
            r["ca__numero_ca"],
            r.get("Validade_CA_Str", "-"),
            dias_ca_val,
            "SIM" if r["item_critico"] else ("SUGERIDO" if r["Sugestao_Critico"] else "NÃO"),
            r["Curva_ABC"],
            r["Padrao_Demanda"],
            int(round(float(r["Qtd_Vendida"]))),
            int(r["Num_Pedidos"]),
            int(r["Num_Clientes"]),
            round(float(r["Vendas_Media_Mes"]), 1),
            round(float(r["Tendencia_Mes"]), 1),
            int(r["estoque_atual"]),
            int(r["qtd_ordem_compra"]),
            int(r["estoque_fisico"]),
            int(r["estoque_minimo"]),
            int(r["Meta_Estoque"]),
            int(r["Teto_Estoque"]),
            round(float(r["Cobertura_Meses"]), 1),
            r["Status"],
            int(r["Qtd_Sugerida_Compra"]),
        ]
        if pode_ver_valores_financeiros:
            linha.extend(
                [
                    round(float(r["Custo_Ref"]), 2),
                    round(float(r["Valor_Compra_Estimado"]), 2),
                ]
            )
        linha.extend([int(r["Qtd_Excesso"]), r["Ultima_Venda_Str"]])
        ws.append(linha)

        # Formatação numérica e visual da linha
        ws.cell(row=row_idx, column=14).number_format = "0.0"
        ws.cell(row=row_idx, column=15).number_format = "+0.0;-0.0;0.0"
        ws.cell(row=row_idx, column=22).number_format = "0.0"
        status_cell = ws.cell(row=row_idx, column=23)
        if r["Status"] in status_fills:
            status_cell.fill = status_fills[r["Status"]]
            status_cell.font = Font(bold=True, size=9)

        if int(r["Qtd_Sugerida_Compra"]) > 0:
            ws.cell(row=row_idx, column=24).font = Font(bold=True, color="DC2626")

        if pode_ver_valores_financeiros:
            ws.cell(row=row_idx, column=25).number_format = '"R$" #,##0.00'
            ws.cell(row=row_idx, column=26).number_format = '"R$" #,##0.00'

        for c_i in range(1, len(cabecalho) + 1):
            ws.cell(row=row_idx, column=c_i).border = thin_border

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 26

    # Ajuste automático de largura das colunas
    for col_idx in range(1, len(cabecalho) + 1):
        col_letter = get_column_letter(col_idx)
        header_len = len(str(cabecalho[col_idx - 1]))
        max_len = header_len
        for row_idx in range(2, min(ws.max_row + 1, 200)):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 45)

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    filename = f"planejamento_estoque_compras_amm_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def gestao_produtos_view(request):
    """
    Página exclusiva de Gestão de Produtos e Certificados de Aprovação (CA).
    Acesso restrito aos perfis COMPRAS, ADMINISTRADOR e ALMOXARIFADO.
    Permite cadastrar/editar: Estoque Mínimo, Estoque Máximo, Categoria, Subcategoria, Tamanho, CA e Marca.
    """
    if not _usuario_pode_gerenciar_produtos(request.user):
        messages.warning(
            request,
            "Acesso restrito: a página de Cadastro e Edição de Produtos é exclusiva para os perfis Compras, Almoxarifado e Administrador.",
        )
        return redirect("dashboard_vendas")

    hoje = timezone.now().date()
    limite_a_vencer = hoje + timedelta(days=90)

    busca = request.GET.get("q", "").strip()
    categoria_filtro = request.GET.get("categoria", "TODAS").strip()
    subcategoria_filtro = request.GET.get("subcategoria", "TODAS").strip()
    marca_filtro = request.GET.get("marca", "TODAS").strip()
    filtro_ca = request.GET.get("filtro_ca", "TODOS").strip()

    base_qs = ProdutoEPI.objects.select_related("categoria", "ca").all()

    # KPIs gerais do cadastro de produtos e CAs
    total_produtos = base_qs.count()
    qtd_com_ca = base_qs.filter(ca__isnull=False).count()
    qtd_sem_ca = base_qs.filter(ca__isnull=True).count()
    qtd_ca_vencido = base_qs.filter(ca__data_validade__lt=hoje).count()
    qtd_ca_a_vencer = base_qs.filter(
        ca__data_validade__gte=hoje,
        ca__data_validade__lte=limite_a_vencer,
    ).count()
    qtd_minimo_config = base_qs.filter(Q(item_critico=True) | Q(estoque_minimo__gt=0)).count()

    # Listas para filtros e autocompletar nos modais
    categorias_disponiveis = list(
        Categoria.objects.order_by("nome").values_list("nome", flat=True)
    )
    subcategorias_disponiveis = sorted(
        {
            s.strip()
            for s in base_qs.exclude(subcategoria__isnull=True)
            .exclude(subcategoria="")
            .values_list("subcategoria", flat=True)
            if s and s.strip()
        }
    )
    marcas_disponiveis = sorted(
        {
            m.strip()
            for m in base_qs.exclude(marca__isnull=True)
            .exclude(marca="")
            .values_list("marca", flat=True)
            if m and m.strip() and m.strip().upper() != "N/A"
        }
    )
    tamanhos_disponiveis = sorted(
        {
            t.strip()
            for t in base_qs.exclude(tamanho_variacao__isnull=True)
            .exclude(tamanho_variacao="")
            .values_list("tamanho_variacao", flat=True)
            if t and t.strip()
        }
    )

    # Aplicação dos filtros na listagem
    qs_filtrado = base_qs
    if busca:
        qs_filtrado = qs_filtrado.filter(
            Q(sku__icontains=busca)
            | Q(nome__icontains=busca)
            | Q(marca__icontains=busca)
            | Q(ca__numero_ca__icontains=busca)
            | Q(subcategoria__icontains=busca)
        )
    if categoria_filtro and categoria_filtro != "TODAS":
        qs_filtrado = qs_filtrado.filter(categoria__nome=categoria_filtro)
    if subcategoria_filtro and subcategoria_filtro != "TODAS":
        qs_filtrado = qs_filtrado.filter(subcategoria=subcategoria_filtro)
    if marca_filtro and marca_filtro != "TODAS":
        qs_filtrado = qs_filtrado.filter(marca=marca_filtro)

    if filtro_ca == "COM_CA":
        qs_filtrado = qs_filtrado.filter(ca__isnull=False)
    elif filtro_ca == "SEM_CA":
        qs_filtrado = qs_filtrado.filter(ca__isnull=True)
    elif filtro_ca == "CA_VENCIDO":
        qs_filtrado = qs_filtrado.filter(ca__data_validade__lt=hoje)
    elif filtro_ca == "CA_A_VENCER":
        qs_filtrado = qs_filtrado.filter(
            ca__data_validade__gte=hoje,
            ca__data_validade__lte=limite_a_vencer,
        )
    elif filtro_ca == "CA_VALIDO":
        qs_filtrado = qs_filtrado.filter(ca__data_validade__gt=limite_a_vencer)
    elif filtro_ca == "EDITADOS":
        qs_filtrado = qs_filtrado.filter(editado_manualmente=True)

    produtos = list(qs_filtrado.order_by("nome", "sku"))
    cas_cadastrados = list(CertificadoAprovacao.objects.order_by("numero_ca"))

    context = {
        "produtos": produtos,
        "cas_cadastrados": cas_cadastrados,
        "unidades_choices": ProdutoEPI.UNIDADE_MEDIDA_CHOICES,
        "categorias_disponiveis": categorias_disponiveis,
        "subcategorias_disponiveis": subcategorias_disponiveis,
        "marcas_disponiveis": marcas_disponiveis,
        "tamanhos_disponiveis": tamanhos_disponiveis,
        "busca": busca,
        "categoria_filtro": categoria_filtro,
        "subcategoria_filtro": subcategoria_filtro,
        "marca_filtro": marca_filtro,
        "filtro_ca": filtro_ca,
        "kpis": {
            "total_produtos": total_produtos,
            "qtd_com_ca": qtd_com_ca,
            "qtd_sem_ca": qtd_sem_ca,
            "qtd_ca_vencido": qtd_ca_vencido,
            "qtd_ca_a_vencer": qtd_ca_a_vencer,
            "qtd_minimo_config": qtd_minimo_config,
        },
    }
    return render(request, "inventory/gestao_produtos.html", context)


@login_required
@require_POST
def salvar_produto_view(request):
    """
    Cria ou edita um ProdutoEPI (Estoque Mínimo, Estoque Máximo, Categoria, Subcategoria,
    Tamanho, CA e Marca).
    Sempre que o CA é informado ou editado, roda automaticamente a consulta e atualização
    do CA específico daquele item.
    """
    if not _usuario_pode_gerenciar_produtos(request.user):
        messages.error(request, "Sem permissão para editar produtos.")
        return redirect("dashboard_estoque")

    produto_id = request.POST.get("produto_id", "").strip()
    sku = request.POST.get("sku", "").strip().upper()
    nome = request.POST.get("nome", "").strip()
    marca = request.POST.get("marca", "").strip().upper() or "N/A"
    categoria_nome = request.POST.get("categoria", "").strip().upper() or "GERAL"
    subcategoria = request.POST.get("subcategoria", "").strip().upper() or None
    tamanho_variacao = request.POST.get("tamanho_variacao", "").strip() or None
    unidade_medida = request.POST.get("unidade_medida", "UN").strip().upper() or "UN"
    numero_ca_raw = request.POST.get("numero_ca", "").strip()
    item_critico = request.POST.get("item_critico") in ("on", "true", "1", "True")

    try:
        estoque_minimo = max(0, int(request.POST.get("estoque_minimo", 0) or 0))
    except ValueError:
        estoque_minimo = 0

    try:
        estoque_maximo = max(0, int(request.POST.get("estoque_maximo", 0) or 0))
    except ValueError:
        estoque_maximo = 0

    if not sku:
        messages.error(request, "O código (SKU) do produto é obrigatório.")
        return redirect(_obter_redirect_seguro(request, "/inventory/produtos/"))

    categoria_obj, _ = Categoria.objects.get_or_create(
        nome=categoria_nome,
        defaults={"descricao": f"Categoria cadastrada via Gestão de Produtos: {categoria_nome}"},
    )

    if produto_id:
        produto = ProdutoEPI.objects.select_related("ca").filter(pk=produto_id).first()
        if not produto:
            messages.error(request, "Produto não encontrado para edição.")
            return redirect(_obter_redirect_seguro(request, "/inventory/produtos/"))
    else:
        produto = ProdutoEPI.objects.select_related("ca").filter(sku=sku).first()

    ca_anterior = produto.ca.numero_ca if (produto and produto.ca) else None
    ca_limpo = ConsultaCAClient.limpar_numero_ca(numero_ca_raw) if numero_ca_raw else ""

    ca_obj = None
    msg_ca_extra = ""

    if ca_limpo:
        # Sempre que o CA for editado (ou se ainda não possuir validade), rerroda a atualização do CA específico
        ca_foi_editado = (ca_limpo != ca_anterior)
        ca_existente = CertificadoAprovacao.objects.filter(numero_ca=ca_limpo).first()

        if ca_foi_editado or not ca_existente or not ca_existente.data_validade:
            res_ca = sincronizar_vencimentos_ca(
                forcar_todos=True,
                numero_ca_especifico=ca_limpo,
                max_workers=1,
            )
            ca_obj = CertificadoAprovacao.objects.filter(numero_ca=ca_limpo).first()
            if ca_obj and ca_obj.data_validade:
                dias_rest = ca_obj.dias_para_vencer
                msg_ca_extra = (
                    f" | CA {ca_limpo} consultado e atualizado automaticamente: "
                    f"validade {ca_obj.data_validade.strftime('%d/%m/%Y')} ({dias_rest} dias - {ca_obj.get_status_display()})."
                )
            else:
                msg_ca_extra = (
                    f" | CA {ca_limpo} vinculado, porém não foi localizado na base online do MTE. "
                    f"Se necessário, informe a validade manualmente em 'Cadastrar / Consultar CA'."
                )
        else:
            ca_obj = ca_existente
            if ca_obj.data_validade:
                msg_ca_extra = f" | CA {ca_limpo} (Validade: {ca_obj.data_validade.strftime('%d/%m/%Y')})."

    if produto:
        produto.sku = sku
        if nome:
            produto.nome = nome
        produto.marca = marca
        produto.categoria = categoria_obj
        produto.subcategoria = subcategoria
        produto.tamanho_variacao = tamanho_variacao
        produto.unidade_medida = unidade_medida
        produto.ca = ca_obj
        produto.estoque_minimo = estoque_minimo
        produto.estoque_maximo = estoque_maximo
        produto.item_critico = item_critico
        produto.editado_manualmente = True
        produto.save()
        acao_str = "atualizado"
    else:
        produto = ProdutoEPI.objects.create(
            sku=sku,
            nome=nome or sku,
            marca=marca,
            categoria=categoria_obj,
            subcategoria=subcategoria,
            tamanho_variacao=tamanho_variacao,
            unidade_medida=unidade_medida,
            ca=ca_obj,
            estoque_minimo=estoque_minimo,
            estoque_maximo=estoque_maximo,
            item_critico=item_critico,
            editado_manualmente=True,
        )
        acao_str = "cadastrado"

    messages.success(
        request,
        f"Produto {produto.sku} ({produto.nome}) {acao_str} com sucesso! "
        f"[Categoria: {categoria_obj.nome}"
        f"{f' / {subcategoria}' if subcategoria else ''} | "
        f"Marca: {marca} | Tam: {tamanho_variacao or '-'} | "
        f"Mín: {estoque_minimo} | Máx: {estoque_maximo}]{msg_ca_extra}",
    )
    return redirect(_obter_redirect_seguro(request, "/inventory/produtos/"))


@login_required
@require_POST
def salvar_ou_consultar_ca_view(request):
    """
    Permite cadastrar um novo CA ou forçar a reconsulta de um CA específico na base MTE,
    com suporte opcional a preenchimento manual de data de validade e fabricante.
    """
    if not _usuario_pode_gerenciar_produtos(request.user):
        messages.error(request, "Sem permissão para gerenciar CAs.")
        return redirect("dashboard_estoque")

    numero_ca_raw = request.POST.get("numero_ca", "").strip()
    ca_limpo = ConsultaCAClient.limpar_numero_ca(numero_ca_raw)
    if not ca_limpo:
        messages.error(request, "Informe um número de CA válido.")
        return redirect(_obter_redirect_seguro(request, "/inventory/produtos/"))

    data_manual_str = request.POST.get("data_validade_manual", "").strip()
    fabricante_manual = request.POST.get("fabricante_manual", "").strip()
    descricao_manual = request.POST.get("descricao_manual", "").strip()
    sku_vincular = request.POST.get("sku_vincular", "").strip().upper()

    # Roda a consulta oficial do CA específico
    sincronizar_vencimentos_ca(
        forcar_todos=True,
        numero_ca_especifico=ca_limpo,
        max_workers=1,
    )

    ca_obj = CertificadoAprovacao.objects.filter(numero_ca=ca_limpo).first()

    # Se o usuário informou dados manuais (ex: correção manual de validade/fabricante), aplica por cima
    if ca_obj and (data_manual_str or fabricante_manual or descricao_manual):
        if data_manual_str:
            try:
                dt_man = datetime.strptime(data_manual_str, "%Y-%m-%d").date()
                ca_obj.data_validade = dt_man
                ca_obj.status = "VENCIDO" if dt_man < timezone.now().date() else "VALIDO"
            except ValueError:
                pass
        if fabricante_manual:
            ca_obj.fabricante = fabricante_manual[:255]
        if descricao_manual:
            ca_obj.descricao_equipamento = descricao_manual
        ca_obj.save()

    msg_vinculo = ""
    if sku_vincular and ca_obj:
        prod = ProdutoEPI.objects.filter(sku=sku_vincular).first()
        if prod:
            prod.ca = ca_obj
            prod.editado_manualmente = True
            prod.save(update_fields=["ca", "editado_manualmente", "data_atualizacao"])
            msg_vinculo = f" e vinculado ao produto {prod.sku}"

    if ca_obj and ca_obj.data_validade:
        messages.success(
            request,
            f"CA {ca_obj.numero_ca} atualizado com sucesso{msg_vinculo}! "
            f"Validade: {ca_obj.data_validade.strftime('%d/%m/%Y')} ({ca_obj.dias_para_vencer} dias) | "
            f"Status: {ca_obj.get_status_display()} | Fabricante: {ca_obj.fabricante}.",
        )
    else:
        messages.warning(
            request,
            f"CA {ca_limpo} registrado{msg_vinculo}, mas não retornou data de validade automática na base MTE. "
            f"Você pode informar a data de validade manualmente caso necessário.",
        )

    return redirect(_obter_redirect_seguro(request, "/inventory/produtos/"))


