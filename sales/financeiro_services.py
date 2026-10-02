import calendar
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    ClassificacaoCusto,
    ConfiguracaoFinanceira,
    ContaPagar,
    ContaReceber,
    NotaFiscal,
)

MESES_NOMES_PT = {
    1: "Janeiro",
    2: "Fevereiro",
    3: "Março",
    4: "Abril",
    5: "Maio",
    6: "Junho",
    7: "Julho",
    8: "Agosto",
    9: "Setembro",
    10: "Outubro",
    11: "Novembro",
    12: "Dezembro",
}

MESES_CURTOS_PT = {
    1: "Jan",
    2: "Fev",
    3: "Mar",
    4: "Abr",
    5: "Mai",
    6: "Jun",
    7: "Jul",
    8: "Ago",
    9: "Set",
    10: "Out",
    11: "Nov",
    12: "Dez",
}


def fmt_brl(valor: float) -> str:
    """Formata número float no padrão monetário brasileiro R$ X.XXX,XX."""
    try:
        v = float(valor or 0.0)
    except (ValueError, TypeError):
        v = 0.0
    sinal = "-" if v < 0 else ""
    v_abs = abs(v)
    inteiro = int(v_abs)
    centavos = int(round((v_abs - inteiro) * 100))
    if centavos == 100:
        inteiro += 1
        centavos = 0
    int_str = f"{inteiro:,}".replace(",", ".")
    return f"{sinal}R$ {int_str},{centavos:02d}"


def fmt_pct(valor: float, casas: int = 1, com_sinal: bool = False) -> str:
    try:
        v = float(valor or 0.0)
    except (ValueError, TypeError):
        v = 0.0
    sinal = "+" if (com_sinal and v > 0) else ""
    return f"{sinal}{v:.{casas}f}%".replace(".", ",")


def fig_to_html(fig) -> str:
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Inter, system-ui, -apple-system, sans-serif", size=12, color="#1e293b"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        config={"displayModeBar": False, "responsive": True},
    )


def _adicionar_meses(ano: int, mes: int, delta: int) -> tuple[int, int]:
    idx = (ano * 12 + (mes - 1)) + delta
    return idx // 12, (idx % 12) + 1


def eh_transferencia_intercompany(
    nome_parte: str = "",
    documento_parte: str = "",
    grupo_conta: str = "",
    subconta: str = "",
    observacao: str = "",
) -> bool:
    """
    Identifica transferências financeiras internas entre AMM EPIs e AMM Soluções.
    No consolidado ('TODAS'), essas transferências não devem inflar artificialmente
    as entradas ou saídas operacionais (Seção 17).
    """
    nome = (nome_parte or "").strip().upper()
    gc = (grupo_conta or "").strip().upper()
    sc = (subconta or "").strip().upper()
    obs = (observacao or "").strip().upper()
    texto_conta = f"{gc} | {sc} | {obs}"

    termos_transf = (
        "TRANSFERENCIA ENTRE EMPRESAS",
        "TRANSFERÊNCIA ENTRE EMPRESAS",
        "TRANSF ENTRE EMPRESAS",
        "TRANSFERENCIA INTERNA",
        "TRANSFERÊNCIA INTERNA",
        "MUTUO ENTRE EMPRESAS",
        "MÚTUO ENTRE EMPRESAS",
        "INTERCOMPANY",
    )
    if any(t in texto_conta for t in termos_transf):
        return True

    # Entidades jurídicas do próprio grupo AMM (excluindo cartões de combustível e funcionários)
    if "COMBUSTIVEL" in nome or "FUNCIONARIO" in nome or "GAMMA" in nome:
        return False

    entidades_amm = (
        "AMM COMERCIO DE EQUIPAMENTOS",
        "AMM COMÉRCIO DE EQUIPAMENTOS",
        "AMM SOLUCOES EM EPIS",
        "AMM SOLUÇÕES EM EPIS",
    )
    if any(ent in nome for ent in entidades_amm):
        return True

    if nome in ("AMM EPIS", "AMM EPI'S", "AMM SOLUCOES", "AMM SOLUÇÕES", "AMM EPIS LTDA", "AMM SOLUCOES LTDA"):
        return True

    return False


def obter_configuracao_financeira(empresa_filtro: str = "TODAS") -> ConfiguracaoFinanceira:
    """
    Recupera (ou inicializa) a configuração de Saldo Bancário Manual e Reserva Mínima
    para o escopo selecionado. Se a empresa específica ainda não tiver registro próprio,
    utiliza ou cria com Reserva Mínima padrão de R$ 30.000,00.
    """
    escopo = (empresa_filtro or "TODAS").strip() or "TODAS"
    cfg = ConfiguracaoFinanceira.objects.filter(empresa__iexact=escopo).first()
    if not cfg:
        cfg_todas = ConfiguracaoFinanceira.objects.filter(empresa="TODAS").first()
        saldo_padrao = Decimal("150000.00") if escopo == "TODAS" and not cfg_todas else Decimal("0.00")
        cfg = ConfiguracaoFinanceira.objects.create(
            empresa=escopo,
            saldo_bancario_atual=saldo_padrao,
            reserva_minima=Decimal("30000.00"),
            alerta_aumento_pct=Decimal("10.00"),
            alerta_aumento_valor=Decimal("500.00"),
            usuario_atualizacao="Sistema (Inicial)",
        )
    return cfg


def inferir_classificacao_padrao(centro_custo: str, grupo_conta: str, subconta: str) -> dict:
    """
    Regra padrão inteligente para classificar inicialmente qualquer conta do ERP em:
    - categoria (Categoria Gerencial)
    - subcategoria (Subcategoria)
    - tipo_custo: CMV | FIXO | VARIAVEL | FINANCEIRO | EXTRAORDINARIO
    - recorrente: bool (para Custos Fixos Recorrentes)
    """
    cc = (centro_custo or "").strip().upper()
    gc = (grupo_conta or "").strip().upper()
    sc = (subconta or "").strip().upper()
    chave = sc or gc or cc or "OUTROS"
    texto = f"{cc} | {gc} | {sc}"

    # A. CUSTO DE MERCADORIAS / CMV
    if (
        "COMPRA MERCADORIA" in texto
        or "COMPRA DE MATERIA PRIMA" in texto
        or "MATERIA PRIMA" in texto
        or "ICMS SUB. TRIBUT" in texto
        or "FRETE SOBRE COMPRA" in texto
        or "COMPRA DE CAIXA ECOMMERCE" in texto
    ):
        sub = "Frete / Impostos sobre Compra" if ("FRETE" in sc or "ICMS" in sc) else "Mercadoria para Revenda / Insumos"
        return {
            "categoria": "Mercadorias / CMV",
            "subcategoria": sub,
            "tipo_custo": "CMV",
            "recorrente": False,
        }

    # D. DESPESAS FINANCEIRAS
    if (
        "TARIFAS BANCARIAS" in texto
        or "TAXA DE RELACIONAMENTO BANCO" in texto
        or "TAXA BANCO" in texto
        or "IOF" in texto
        or "JUROS" in texto
        or "ANTECIPACAO" in texto
    ):
        return {
            "categoria": "Despesas Financeiras",
            "subcategoria": sc.title() if sc else "Tarifas e Taxas Bancárias",
            "tipo_custo": "FINANCEIRO",
            "recorrente": True,
        }

    # E. DESPESAS EXTRAORDINÁRIAS / INVESTIMENTOS
    if "RECISÕES" in texto or "RESCIS" in texto or "IMOBILIZADO" in texto or "BENFEITORIA" in texto:
        return {
            "categoria": "Despesas Extraordinárias",
            "subcategoria": sc.title() if sc else "Não Recorrente / Investimento",
            "tipo_custo": "EXTRAORDINARIO",
            "recorrente": False,
        }

    # C. CUSTOS VARIÁVEIS — Comissões
    if "COMISS" in texto:
        return {
            "categoria": "Comissões",
            "subcategoria": sc.title() if sc else "Comissão sobre Vendas",
            "tipo_custo": "VARIAVEL",
            "recorrente": False,
        }

    # C. CUSTOS VARIÁVEIS — Bonificações
    if "BONIFICA" in texto or "BONUS E GRATIFICACOES" in texto:
        return {
            "categoria": "Bonificações",
            "subcategoria": sc.title() if sc else "Bonificação / Premiação",
            "tipo_custo": "VARIAVEL",
            "recorrente": False,
        }

    # C. CUSTOS VARIÁVEIS — Fretes sobre vendas / Transportadoras
    if "FRETE" in texto:
        return {
            "categoria": "Fretes / Transportadoras",
            "subcategoria": sc.title() if sc else "Frete sobre Vendas",
            "tipo_custo": "VARIAVEL",
            "recorrente": False,
        }

    # C. CUSTOS VARIÁVEIS — Impostos sobre vendas (Simples, ICMS, IRRF/PIS/COFINS, Taxas)
    if (
        "SIMPLES NACIONAL" in texto
        or ("ICMS" in texto and "COMPRA" not in texto)
        or "PIS/COFINS" in texto
        or "TAXAS DIVERSAS" in texto
        or "DARF" in texto
    ):
        return {
            "categoria": "Impostos",
            "subcategoria": sc.title() if sc else "Impostos sobre Vendas",
            "tipo_custo": "VARIAVEL",
            "recorrente": False,
        }

    # C. CUSTOS VARIÁVEIS — Marketing / Viagens Comerciais / Devoluções
    if "MARKETING" in texto or "PUBLICIDADE" in texto:
        return {
            "categoria": "Marketing",
            "subcategoria": sc.title() if sc else "Publicidade e Propaganda",
            "tipo_custo": "VARIAVEL",
            "recorrente": False,
        }

    if "VIAGENS" in texto or "DEVOLUCAO DE VENDA" in texto:
        return {
            "categoria": "Comercial / Vendas",
            "subcategoria": sc.title() if sc else "Despesas Comerciais Variáveis",
            "tipo_custo": "VARIAVEL",
            "recorrente": False,
        }

    # B. CUSTOS FIXOS — Pró-Labore / Sócios
    if "SOCIOS" in texto or "PRO LABORE" in texto or "RETIRADA SOCIO" in texto or cc == "DIRETORIA":
        return {
            "categoria": "Pró-Labore / Sócios",
            "subcategoria": sc.title() if sc else "Pró-Labore e Retiradas",
            "tipo_custo": "FIXO",
            "recorrente": True,
        }

    # B. CUSTOS FIXOS — Pessoal / Folha
    if (
        "SALARIO" in texto
        or "FERIAS" in texto
        or "FGTS" in texto
        or "INSS" in texto
        or "ASSIST. MEDICA" in texto
        or "ASSISTENCIA MÉDICA" in texto
        or "ALIMENTACAO" in texto
        or "VALE TRANSPORTE" in texto
        or "UNIFORMES" in texto
        or "CAPACITACAO" in texto
        or "TREINAMENTO" in texto
    ):
        rec = "FERIAS" not in texto and "CAPACITACAO" not in texto and "TREINAMENTO" not in texto
        return {
            "categoria": "Pessoal / Folha",
            "subcategoria": sc.title() if sc else "Salários e Encargos",
            "tipo_custo": "FIXO",
            "recorrente": rec,
        }

    # B. CUSTOS FIXOS — Aluguel / Estrutura
    if (
        "ALUGUEL" in texto
        or "ALUGUEIS" in texto
        or "ENERGIA" in texto
        or "COPEL" in texto
        or "ÁGUA" in texto
        or "AGUA E SANEAMENTO" in texto
        or "MANUTENCAO ESTRUTURAL" in texto
        or "SEGUROS" in texto
    ):
        rec = "MANUTENCAO" not in texto
        return {
            "categoria": "Aluguel / Estrutura",
            "subcategoria": sc.title() if sc else "Ocupação e Utilidades",
            "tipo_custo": "FIXO",
            "recorrente": rec,
        }

    # B. CUSTOS FIXOS — Sistemas / TI
    if (
        "SISTEMA ERP" in texto
        or "CRM" in texto
        or "SOFTWARE" in texto
        or "INFORMATICA" in texto
        or "HOSPEDAGEM EMAIL" in texto
    ):
        return {
            "categoria": "Sistemas / TI",
            "subcategoria": sc.title() if sc else "Sistemas e Licenças",
            "tipo_custo": "FIXO",
            "recorrente": True,
        }

    # B. CUSTOS FIXOS — Veículos / Combustível
    if (
        "COMBUSTIVEIS" in texto
        or "COMBUSTIVEL" in texto
        or "VEICULO" in texto
        or "VEÍCULO" in texto
        or "RASTREADOR" in texto
        or "TRANSPORTE (ADM)" in texto
    ):
        sub = "Combustível / Gasolina" if "COMBUST" in texto else (sc.title() if sc else "Frota e Veículos")
        rec = "RASTREADOR" in texto or "COMBUST" in texto
        return {
            "categoria": "Veículos",
            "subcategoria": sub,
            "tipo_custo": "FIXO",
            "recorrente": rec,
        }

    # B. CUSTOS FIXOS — Serviços / Contabilidade
    if (
        "CONTABILIDADE" in texto
        or "ASSESSORIA" in texto
        or "SERVICOS DE TERCEIROS" in texto
        or "ENTIDADES DE CLASSE" in texto
    ):
        rec = "CONTABILIDADE" in texto or "ASSESSORIA" in texto or "ENTIDADES" in texto
        return {
            "categoria": "Serviços / Contabilidade",
            "subcategoria": sc.title() if sc else "Contabilidade e Terceiros",
            "tipo_custo": "FIXO",
            "recorrente": rec,
        }

    # B. CUSTOS FIXOS — Administrativo (Telefonia, Internet, Escritório, Copa, Locação)
    rec_adm = any(k in texto for k in ("TELEFONIA", "INTERNET", "PLANO DE CELULAR", "LOCACAO"))
    return {
        "categoria": "Administrativo",
        "subcategoria": chave.title(),
        "tipo_custo": "FIXO",
        "recorrente": rec_adm,
    }


def sincronizar_mapa_classificacao_custos() -> dict[str, dict]:
    """
    Garante que todas as contas existentes em ContaPagar possuam registro em ClassificacaoCusto
    e retorna um dicionário {conta_chave: {categoria, subcategoria, tipo_custo, recorrente, ...}}
    para consulta O(1) em memória.
    """
    existentes = {
        c.conta_chave.strip().upper(): c
        for c in ClassificacaoCusto.objects.all()
    }

    combos = (
        ContaPagar.objects.filter(cancelada=False)
        .exclude(status="CANCELADO")
        .values_list("centro_custo", "grupo_conta", "subconta")
        .distinct()
    )

    novos = []
    vistos_novos = set()
    for cc, gc, sc in combos:
        chave = (sc or gc or cc or "OUTROS").strip().upper()
        if not chave:
            chave = "OUTROS"
        if chave not in existentes and chave not in vistos_novos:
            padrao = inferir_classificacao_padrao(cc, gc, sc)
            obj = ClassificacaoCusto(
                conta_chave=chave,
                grupo_conta_erp=(gc or "").strip().upper() or None,
                centro_custo_erp=(cc or "").strip().upper() or None,
                categoria=padrao["categoria"],
                subcategoria=padrao["subcategoria"],
                tipo_custo=padrao["tipo_custo"],
                recorrente=padrao["recorrente"],
                editado_manualmente=False,
                atualizado_por="Auto-Classificador AMM",
            )
            novos.append(obj)
            vistos_novos.add(chave)

    if novos:
        ClassificacaoCusto.objects.bulk_create(novos, ignore_conflicts=True)
        existentes = {
            c.conta_chave.strip().upper(): c
            for c in ClassificacaoCusto.objects.all()
        }

    mapa = {}
    for chave, obj in existentes.items():
        mapa[chave] = {
            "id": obj.id,
            "conta_chave": obj.conta_chave,
            "grupo_conta_erp": obj.grupo_conta_erp or "-",
            "centro_custo_erp": obj.centro_custo_erp or "-",
            "categoria": obj.categoria,
            "subcategoria": obj.subcategoria or "-",
            "tipo_custo": obj.tipo_custo,
            "tipo_custo_display": obj.get_tipo_custo_display(),
            "recorrente": bool(obj.recorrente),
            "editado_manualmente": bool(obj.editado_manualmente),
            "atualizado_por": obj.atualizado_por or "Sistema",
        }
    return mapa


def gerar_fluxo_caixa_e_liquidez(
    mes_selecionado: int,
    ano_selecionado: int,
    horizonte_meses: int = 3,
    empresa_filtro: str = "TODAS",
    portador_filtro: str = "TODOS",
    status_filtro: str = "TODOS",
    simulacao_valor: float = 0.0,
    simulacao_vencimento: str = "",
    gerar_graficos: bool = True,
) -> dict:
    """
    Calcula toda a estrutura da Primeira Tela do Financeiro (Seções 1 a 20):
    - Saldo Bancário Manual Informado, Reserva Mínima (R$ 30.000,00) e Disponível
    - Projeção cumulativa de Fluxo de Caixa para os próximos 3, 6 ou 12 meses
    - Simulador de impacto de novas compras considerando a data/mês de vencimento
    - Fluxo de Caixa Diário do mês selecionado (Entradas, Saídas, Saldo Projetado, Risco Diário)
    - Contas a Receber e Contas a Pagar do mês selecionado + Diagnóstico 234 vs 242 títulos
    - Resumo de Cobrança, Envelhecimento (A vencer, 1-30d, >30d), Taxa de Inadimplência e Lista de Cobrança
    - Alertas Financeiros e Semáforo Geral (Saudável / Atenção / Ação Necessária)
    """
    hoje = timezone.now().date()
    cfg = obter_configuracao_financeira(empresa_filtro)
    saldo_bancario_informado = float(cfg.saldo_bancario_atual or 0.0)
    reserva_minima = float(cfg.reserva_minima or 30000.0)

    if horizonte_meses not in (3, 6, 12):
        horizonte_meses = 3

    # Base de Contas a Receber e Contas a Pagar (excluindo canceladas)
    cr_qs = ContaReceber.objects.filter(cancelada=False).exclude(status="CANCELADO")
    cp_qs = ContaPagar.objects.filter(cancelada=False).exclude(status="CANCELADO")

    if empresa_filtro and empresa_filtro != "TODAS":
        cr_qs = cr_qs.filter(empresa=empresa_filtro)
        cp_qs = cp_qs.filter(empresa=empresa_filtro)

    if portador_filtro and portador_filtro != "TODOS":
        cr_qs = cr_qs.filter(portador__iexact=portador_filtro)
        cp_qs = cp_qs.filter(portador__iexact=portador_filtro)

    cr_todos = list(
        cr_qs.values(
            "id",
            "id_titulo_erp",
            "empresa",
            "numero_documento",
            "numero_duplicata",
            "parcela",
            "cliente_nome",
            "cliente_documento",
            "vendedor_nome",
            "data_emissao",
            "data_vencimento",
            "data_recebimento",
            "dias_atraso",
            "valor_total",
            "valor_recebido",
            "valor_saldo",
            "portador",
            "subconta",
            "grupo_conta",
            "nosso_numero",
            "observacao",
            "status",
        )
    )

    cp_todos = list(
        cp_qs.values(
            "id",
            "id_titulo_erp",
            "empresa",
            "numero_documento",
            "numero_duplicata",
            "parcela",
            "fornecedor_nome",
            "fornecedor_documento",
            "data_emissao",
            "data_vencimento",
            "data_pagamento",
            "dias_atraso",
            "valor_total",
            "valor_pago",
            "valor_saldo",
            "centro_custo",
            "subconta",
            "grupo_conta",
            "portador",
            "observacao",
            "status",
        )
    )

    # Neutralização de Transferências entre Empresas no Consolidado (Seção 17)
    consolidado = (not empresa_filtro) or empresa_filtro == "TODAS"
    qtd_transf_cr = 0
    val_transf_cr = 0.0
    qtd_transf_cp = 0
    val_transf_cp = 0.0

    cr_operacional = []
    for item in cr_todos:
        if consolidado and eh_transferencia_intercompany(
            nome_parte=item.get("cliente_nome"),
            documento_parte=item.get("cliente_documento"),
            grupo_conta=item.get("grupo_conta"),
            subconta=item.get("subconta"),
            observacao=item.get("observacao"),
        ):
            qtd_transf_cr += 1
            val_transf_cr += float(item.get("valor_total") or 0.0)
            continue
        cr_operacional.append(item)

    cp_operacional = []
    for item in cp_todos:
        if consolidado and eh_transferencia_intercompany(
            nome_parte=item.get("fornecedor_nome"),
            documento_parte=item.get("fornecedor_documento"),
            grupo_conta=item.get("grupo_conta"),
            subconta=item.get("subconta"),
            observacao=item.get("observacao"),
        ):
            qtd_transf_cp += 1
            val_transf_cp += float(item.get("valor_total") or 0.0)
            continue
        cp_operacional.append(item)

    # -------------------------------------------------------------------------
    # 1. PROJEÇÃO MENSAL DE FLUXO DE CAIXA (PRÓXIMOS 3, 6 OU 12 MESES)
    # -------------------------------------------------------------------------
    # O horizonte inicia no mês/ano selecionado no filtro para que o gestor possa
    # projetar a partir do mês em foco (ex: Outubro, Novembro, Dezembro).
    meses_projecao_coords = [
        _adicionar_meses(ano_selecionado, mes_selecionado, i)
        for i in range(horizonte_meses)
    ]

    # Agrupamento mensal (previsto em aberto vs realizado)
    cr_por_mes_venc = defaultdict(lambda: {"previsto": 0.0, "realizado_venc": 0.0, "total_venc": 0.0, "qtd_previsto": 0, "qtd_total": 0, "qtd_realizado_antecipado": 0, "val_realizado_antecipado": 0.0})
    cp_por_mes_venc = defaultdict(lambda: {"previsto": 0.0, "realizado_venc": 0.0, "total_venc": 0.0, "qtd_previsto": 0, "qtd_total": 0})
    cr_realizado_caixa_mes = defaultdict(float)
    cp_realizado_caixa_mes = defaultdict(float)

    for r in cr_operacional:
        dt_v = r["data_vencimento"]
        dt_r = r["data_recebimento"]
        st = r["status"]
        v_tot = float(r["valor_total"] or 0.0)
        v_rec = float(r["valor_recebido"] or 0.0)
        v_sal = float(r["valor_saldo"] or 0.0)
        val_aberto = (v_sal if v_sal > 0 else v_tot) if st != "RECEBIDO" else 0.0
        val_efetivo_rec = v_rec if v_rec > 0 else (v_tot if st == "RECEBIDO" else 0.0)

        if dt_v:
            ym = (dt_v.year, dt_v.month)
            cr_por_mes_venc[ym]["total_venc"] += v_tot
            cr_por_mes_venc[ym]["qtd_total"] += 1
            if st != "RECEBIDO":
                cr_por_mes_venc[ym]["previsto"] += val_aberto
                cr_por_mes_venc[ym]["qtd_previsto"] += 1
            else:
                cr_por_mes_venc[ym]["realizado_venc"] += val_efetivo_rec
                if dt_r and (dt_r.year, dt_r.month) < ym:
                    cr_por_mes_venc[ym]["qtd_realizado_antecipado"] += 1
                    cr_por_mes_venc[ym]["val_realizado_antecipado"] += val_efetivo_rec

        if dt_r and st == "RECEBIDO":
            cr_realizado_caixa_mes[(dt_r.year, dt_r.month)] += val_efetivo_rec

    for p in cp_operacional:
        dt_v = p["data_vencimento"]
        dt_p = p["data_pagamento"]
        st = p["status"]
        v_tot = float(p["valor_total"] or 0.0)
        v_pag = float(p["valor_pago"] or 0.0)
        v_sal = float(p["valor_saldo"] or 0.0)
        val_aberto = (v_sal if v_sal > 0 else v_tot) if st != "PAGO" else 0.0
        val_efetivo_pag = v_pag if v_pag > 0 else (v_tot if st == "PAGO" else 0.0)

        if dt_v:
            ym = (dt_v.year, dt_v.month)
            cp_por_mes_venc[ym]["total_venc"] += v_tot
            cp_por_mes_venc[ym]["qtd_total"] += 1
            if st != "PAGO":
                cp_por_mes_venc[ym]["previsto"] += val_aberto
                cp_por_mes_venc[ym]["qtd_previsto"] += 1
            else:
                cp_por_mes_venc[ym]["realizado_venc"] += val_efetivo_pag

        if dt_p and st == "PAGO":
            cp_realizado_caixa_mes[(dt_p.year, dt_p.month)] += val_efetivo_pag

    projecao_mensal = []
    saldo_corrente = saldo_bancario_informado
    meses_abaixo_reserva = []

    for idx, (y_m, m_m) in enumerate(meses_projecao_coords):
        ym = (y_m, m_m)
        info_cr = cr_por_mes_venc[ym]
        info_cp = cp_por_mes_venc[ym]

        # Se o mês é histórico (anterior ao mês atual e sem títulos em aberto significativos),
        # podemos exibir o realizado; para projeção de caixa a partir do saldo bancário atual,
        # usamos os recebimentos previstos (em aberto) e pagamentos previstos (em aberto).
        eh_historico_fechado = (ym < (hoje.year, hoje.month)) and (info_cr["previsto"] == 0 and info_cp["previsto"] == 0)
        if eh_historico_fechado:
            rec_mes = cr_realizado_caixa_mes[ym] or info_cr["realizado_venc"]
            pag_mes = cp_realizado_caixa_mes[ym] or info_cp["realizado_venc"]
            modo_linha = "REALIZADO"
        else:
            rec_mes = info_cr["previsto"]
            pag_mes = info_cp["previsto"]
            modo_linha = "PREVISTO"

        saldo_inicial_mes = saldo_corrente
        saldo_projetado_mes = saldo_inicial_mes + rec_mes - pag_mes
        disponivel_mes = saldo_projetado_mes - reserva_minima
        abaixo_reserva = saldo_projetado_mes < reserva_minima
        necessidade_adicional = max(reserva_minima - saldo_projetado_mes, 0.0)
        proximo_reserva = (not abaixo_reserva) and (disponivel_mes < (reserva_minima * 0.5))

        nome_mes_ano = f"{MESES_NOMES_PT[m_m]}/{y_m}"
        linha_proj = {
            "indice": idx + 1,
            "ano": y_m,
            "mes": m_m,
            "mes_nome": MESES_NOMES_PT[m_m],
            "mes_ano_str": nome_mes_ano,
            "modo_linha": modo_linha,
            "saldo_inicial_raw": saldo_inicial_mes,
            "saldo_inicial": fmt_brl(saldo_inicial_mes),
            "recebimentos_previstos_raw": rec_mes,
            "recebimentos_previstos": fmt_brl(rec_mes),
            "qtd_recebimentos": info_cr["qtd_previsto"],
            "realizado_cr_mes": fmt_brl(cr_realizado_caixa_mes[ym]),
            "realizado_cr_mes_raw": cr_realizado_caixa_mes[ym],
            "pagamentos_previstos_raw": pag_mes,
            "pagamentos_previstos": fmt_brl(pag_mes),
            "qtd_pagamentos": info_cp["qtd_previsto"],
            "realizado_cp_mes": fmt_brl(cp_realizado_caixa_mes[ym]),
            "realizado_cp_mes_raw": cp_realizado_caixa_mes[ym],
            "saldo_projetado_raw": saldo_projetado_mes,
            "saldo_projetado": fmt_brl(saldo_projetado_mes),
            "reserva_minima_raw": reserva_minima,
            "reserva_minima": fmt_brl(reserva_minima),
            "disponivel_raw": disponivel_mes,
            "disponivel": fmt_brl(disponivel_mes),
            "abaixo_reserva": abaixo_reserva,
            "proximo_reserva": proximo_reserva,
            "necessidade_adicional_raw": necessidade_adicional,
            "necessidade_adicional": fmt_brl(necessidade_adicional),
            "selecionado": (y_m == ano_selecionado and m_m == mes_selecionado),
        }
        projecao_mensal.append(linha_proj)
        if abaixo_reserva:
            meses_abaixo_reserva.append(linha_proj)

        saldo_corrente = saldo_projetado_mes

    # -------------------------------------------------------------------------
    # 2. SIMULADOR DE NOVAS COMPRAS POR VENCIMENTO (SEÇÃO 7)
    # -------------------------------------------------------------------------
    resultado_simulacao = None
    if simulacao_valor and simulacao_valor > 0:
        sim_ano, sim_mes = ano_selecionado, mes_selecionado
        sim_data_str = f"{mes_selecionado:02d}/{ano_selecionado}"
        if simulacao_vencimento:
            try:
                if len(simulacao_vencimento) == 10 and "-" in simulacao_vencimento:
                    dt_sim = datetime.strptime(simulacao_vencimento, "%Y-%m-%d").date()
                    sim_ano, sim_mes = dt_sim.year, dt_sim.month
                    sim_data_str = dt_sim.strftime("%d/%m/%Y")
                elif len(simulacao_vencimento) == 7 and "-" in simulacao_vencimento:
                    p = simulacao_vencimento.split("-")
                    sim_ano, sim_mes = int(p[0]), int(p[1])
                    sim_data_str = f"{MESES_NOMES_PT[sim_mes]}/{sim_ano}"
            except ValueError:
                pass

        # Encontra o mês correspondente na projeção (ou calcula até ele)
        alvo_proj = next((p for p in projecao_mensal if p["ano"] == sim_ano and p["mes"] == sim_mes), None)
        if not alvo_proj:
            alvo_proj = projecao_mensal[0] if projecao_mensal else None

        if alvo_proj:
            saldo_antes = alvo_proj["saldo_projetado_raw"]
            saldo_apos = saldo_antes - float(simulacao_valor)
            abaixo = saldo_apos < reserva_minima
            deficit = max(reserva_minima - saldo_apos, 0.0)
            folga = max(saldo_apos - reserva_minima, 0.0)
            resultado_simulacao = {
                "valor_compra": fmt_brl(simulacao_valor),
                "valor_compra_raw": float(simulacao_valor),
                "vencimento_str": sim_data_str,
                "mes_ano_str": alvo_proj["mes_ano_str"],
                "saldo_antes": fmt_brl(saldo_antes),
                "saldo_antes_raw": saldo_antes,
                "disponivel_antes": alvo_proj["disponivel"],
                "saldo_apos": fmt_brl(saldo_apos),
                "saldo_apos_raw": saldo_apos,
                "reserva_minima": fmt_brl(reserva_minima),
                "abaixo_reserva": abaixo,
                "deficit": fmt_brl(deficit),
                "deficit_raw": deficit,
                "folga_apos": fmt_brl(folga),
            }

    # -------------------------------------------------------------------------
    # 3. DETALHAMENTO DE CONTAS A RECEBER E CONTAS A PAGAR DO MÊS + DIAGNÓSTICO (SEÇÕES 8, 9, 15)
    # -------------------------------------------------------------------------
    ym_sel = (ano_selecionado, mes_selecionado)
    info_cr_sel = cr_por_mes_venc[ym_sel]
    info_cp_sel = cp_por_mes_venc[ym_sel]

    detalhe_cr_mes = []
    titulos_antecipados_cr = []
    for r in cr_operacional:
        dt_v = r["data_vencimento"]
        dt_r = r["data_recebimento"]
        st = r["status"]
        if not (dt_v and dt_v.year == ano_selecionado and dt_v.month == mes_selecionado):
            continue

        # Aplica filtro de Status se selecionado (Seção 16)
        if status_filtro == "A_VENCER" and st != "A_VENCER":
            continue
        elif status_filtro == "VENCIDO" and st != "VENCIDO":
            continue
        elif status_filtro in ("RECEBIDO", "REALIZADO") and st != "RECEBIDO":
            continue

        v_tot = float(r["valor_total"] or 0.0)
        v_rec = float(r["valor_recebido"] or 0.0)
        v_sal = float(r["valor_saldo"] or 0.0)
        val_efetivo_rec = v_rec if v_rec > 0 else (v_tot if st == "RECEBIDO" else 0.0)
        val_aberto = 0.0 if st == "RECEBIDO" else (v_sal if v_sal > 0 else v_tot)

        recebido_antecipado = bool(st == "RECEBIDO" and dt_r and (dt_r.year, dt_r.month) < ym_sel)
        reg_cr = {
            "id": r["id"],
            "id_titulo_erp": r["id_titulo_erp"],
            "cliente_nome": r["cliente_nome"] or "CLIENTE NÃO IDENTIFICADO",
            "vendedor_nome": r["vendedor_nome"] or "-",
            "empresa": r["empresa"],
            "documento": r["numero_duplicata"] or r["numero_documento"] or r["id_titulo_erp"],
            "parcela": r["parcela"] or "-",
            "data_venc_str": dt_v.strftime("%d/%m/%Y") if dt_v else "-",
            "data_venc_iso": dt_v.strftime("%Y-%m-%d") if dt_v else "",
            "data_rec_str": dt_r.strftime("%d/%m/%Y") if dt_r else "-",
            "data_rec_iso": dt_r.strftime("%Y-%m-%d") if dt_r else "",
            "valor_total": v_tot,
            "valor_total_fmt": fmt_brl(v_tot),
            "valor_recebido": val_efetivo_rec,
            "valor_recebido_fmt": fmt_brl(val_efetivo_rec),
            "valor_aberto": val_aberto,
            "valor_aberto_fmt": fmt_brl(val_aberto),
            "status": st,
            "portador": (r["portador"] or "NÃO INFORMADO").strip().upper(),
            "nosso_numero": r["nosso_numero"] or "-",
            "recebido_antecipado": recebido_antecipado,
        }
        detalhe_cr_mes.append(reg_cr)
        if recebido_antecipado:
            titulos_antecipados_cr.append(reg_cr)

    detalhe_cr_mes.sort(key=lambda x: (x["data_venc_iso"], x["valor_total"]), reverse=True)

    detalhe_cp_mes = []
    for p in cp_operacional:
        dt_v = p["data_vencimento"]
        dt_p = p["data_pagamento"]
        st = p["status"]
        if not (dt_v and dt_v.year == ano_selecionado and dt_v.month == mes_selecionado):
            continue

        if status_filtro == "A_VENCER" and st != "A_VENCER":
            continue
        elif status_filtro == "VENCIDO" and st != "VENCIDO":
            continue
        elif status_filtro in ("RECEBIDO", "REALIZADO", "PAGO") and st != "PAGO":
            continue

        v_tot = float(p["valor_total"] or 0.0)
        v_pag = float(p["valor_pago"] or 0.0)
        v_sal = float(p["valor_saldo"] or 0.0)
        val_efetivo_pag = v_pag if v_pag > 0 else (v_tot if st == "PAGO" else 0.0)
        val_aberto = 0.0 if st == "PAGO" else (v_sal if v_sal > 0 else v_tot)

        detalhe_cp_mes.append(
            {
                "id": p["id"],
                "id_titulo_erp": p["id_titulo_erp"],
                "fornecedor_nome": p["fornecedor_nome"] or "FORNECEDOR NÃO IDENTIFICADO",
                "empresa": p["empresa"],
                "documento": p["numero_duplicata"] or p["numero_documento"] or p["id_titulo_erp"],
                "parcela": p["parcela"] or "-",
                "categoria": p["subconta"] or p["grupo_conta"] or p["centro_custo"] or "-",
                "centro_custo": p["centro_custo"] or "-",
                "data_venc_str": dt_v.strftime("%d/%m/%Y") if dt_v else "-",
                "data_venc_iso": dt_v.strftime("%Y-%m-%d") if dt_v else "",
                "data_pag_str": dt_p.strftime("%d/%m/%Y") if dt_p else "-",
                "data_pag_iso": dt_p.strftime("%Y-%m-%d") if dt_p else "",
                "valor_total": v_tot,
                "valor_total_fmt": fmt_brl(v_tot),
                "valor_pago": val_efetivo_pag,
                "valor_pago_fmt": fmt_brl(val_efetivo_pag),
                "valor_aberto": val_aberto,
                "valor_aberto_fmt": fmt_brl(val_aberto),
                "status": st,
                "portador": (p["portador"] or "NÃO INFORMADO").strip().upper(),
            }
        )

    detalhe_cp_mes.sort(key=lambda x: (x["data_venc_iso"], x["valor_total"]), reverse=True)

    # Diagnóstico transparente da divergência de títulos (Seção 15: ex. 234 pendentes vs 242 totais)
    qtd_baixados_venc_mes = info_cr_sel["qtd_total"] - info_cr_sel["qtd_previsto"]
    val_baixados_venc_mes = info_cr_sel["total_venc"] - info_cr_sel["previsto"]
    diagnostico_titulos_cr = {
        "qtd_total_vencimento": info_cr_sel["qtd_total"],
        "valor_total_vencimento": fmt_brl(info_cr_sel["total_venc"]),
        "qtd_em_aberto": info_cr_sel["qtd_previsto"],
        "valor_em_aberto": fmt_brl(info_cr_sel["previsto"]),
        "qtd_ja_recebidos": qtd_baixados_venc_mes,
        "valor_ja_recebidos": fmt_brl(val_baixados_venc_mes),
        "qtd_antecipados": info_cr_sel["qtd_realizado_antecipado"],
        "valor_antecipados": fmt_brl(info_cr_sel["val_realizado_antecipado"]),
        "titulos_antecipados": titulos_antecipados_cr,
    }

    # -------------------------------------------------------------------------
    # 4. FLUXO DE CAIXA DIÁRIO DO MÊS SELECIONADO (SEÇÕES 10 e 11)
    # -------------------------------------------------------------------------
    ultimo_dia_mes = calendar.monthrange(ano_selecionado, mes_selecionado)[1]
    linha_mes_sel = projecao_mensal[0] if projecao_mensal else None
    saldo_inicial_diario = linha_mes_sel["saldo_inicial_raw"] if linha_mes_sel else saldo_bancario_informado

    entradas_prev_dia = defaultdict(float)
    entradas_real_dia = defaultdict(float)
    saidas_prev_dia = defaultdict(float)
    saidas_real_dia = defaultdict(float)

    for r in cr_operacional:
        dt_v = r["data_vencimento"]
        dt_r = r["data_recebimento"]
        st = r["status"]
        v_tot = float(r["valor_total"] or 0.0)
        v_rec = float(r["valor_recebido"] or 0.0)
        v_sal = float(r["valor_saldo"] or 0.0)
        if dt_v and dt_v.year == ano_selecionado and dt_v.month == mes_selecionado and st != "RECEBIDO":
            entradas_prev_dia[dt_v.day] += (v_sal if v_sal > 0 else v_tot)
        if dt_r and dt_r.year == ano_selecionado and dt_r.month == mes_selecionado and st == "RECEBIDO":
            entradas_real_dia[dt_r.day] += (v_rec if v_rec > 0 else v_tot)

    for p in cp_operacional:
        dt_v = p["data_vencimento"]
        dt_p = p["data_pagamento"]
        st = p["status"]
        v_tot = float(p["valor_total"] or 0.0)
        v_pag = float(p["valor_pago"] or 0.0)
        v_sal = float(p["valor_saldo"] or 0.0)
        if dt_v and dt_v.year == ano_selecionado and dt_v.month == mes_selecionado and st != "PAGO":
            saidas_prev_dia[dt_v.day] += (v_sal if v_sal > 0 else v_tot)
        if dt_p and dt_p.year == ano_selecionado and dt_p.month == mes_selecionado and st == "PAGO":
            saidas_real_dia[dt_p.day] += (v_pag if v_pag > 0 else v_tot)

    fluxo_diario = []
    dias_risco_caixa = []
    dias_concentracao_pagto = []
    saldo_dia_acum = saldo_inicial_diario
    menor_saldo_dia = None

    total_saidas_mes_ref = sum(saidas_prev_dia.values()) or sum(saidas_real_dia.values()) or 1.0
    eh_mes_passado = ym_sel < (hoje.year, hoje.month)

    for dia in range(1, ultimo_dia_mes + 1):
        dt_atual = date(ano_selecionado, mes_selecionado, dia)
        ent_prev = entradas_prev_dia[dia]
        ent_real = entradas_real_dia[dia]
        sai_prev = saidas_prev_dia[dia]
        sai_real = saidas_real_dia[dia]

        # Se for mês histórico fechado, usa realizado; caso contrário, usa previsto em aberto para projeção
        if eh_mes_passado and (sum(entradas_prev_dia.values()) == 0 and sum(saidas_prev_dia.values()) == 0):
            ent_efetiva = ent_real
            sai_efetiva = sai_real
            tipo_dia = "REALIZADO"
        else:
            ent_efetiva = ent_prev
            sai_efetiva = sai_prev
            tipo_dia = "PREVISTO" if dt_atual >= hoje else ("MISTO" if (ent_real > 0 or sai_real > 0) else "PREVISTO")

        saldo_dia_acum = saldo_dia_acum + ent_efetiva - sai_efetiva
        abaixo_res_dia = saldo_dia_acum < reserva_minima
        concentracao_alta = (sai_efetiva >= 25000.0) and (sai_efetiva / total_saidas_mes_ref >= 0.15)

        item_dia = {
            "dia": dia,
            "data_str": f"{dia:02d}/{mes_selecionado:02d}",
            "data_completa_str": dt_atual.strftime("%d/%m/%Y"),
            "entradas_raw": ent_efetiva,
            "entradas": fmt_brl(ent_efetiva),
            "entradas_realizado_raw": ent_real,
            "entradas_realizado": fmt_brl(ent_real),
            "entradas_previsto_raw": ent_prev,
            "entradas_previsto": fmt_brl(ent_prev),
            "saidas_raw": sai_efetiva,
            "saidas": fmt_brl(sai_efetiva),
            "saidas_realizado_raw": sai_real,
            "saidas_realizado": fmt_brl(sai_real),
            "saidas_previsto_raw": sai_prev,
            "saidas_previsto": fmt_brl(sai_prev),
            "saldo_liquido_dia_raw": ent_efetiva - sai_efetiva,
            "saldo_liquido_dia": fmt_brl(ent_efetiva - sai_efetiva),
            "saldo_projetado_raw": saldo_dia_acum,
            "saldo_projetado": fmt_brl(saldo_dia_acum),
            "disponivel_raw": saldo_dia_acum - reserva_minima,
            "disponivel": fmt_brl(saldo_dia_acum - reserva_minima),
            "abaixo_reserva": abaixo_res_dia,
            "concentracao_alta": concentracao_alta,
            "tipo_dia": tipo_dia,
            "tem_movimento": (ent_efetiva > 0 or sai_efetiva > 0 or ent_real > 0 or sai_real > 0),
        }
        fluxo_diario.append(item_dia)

        if menor_saldo_dia is None or saldo_dia_acum < menor_saldo_dia["saldo_projetado_raw"]:
            menor_saldo_dia = item_dia

        if abaixo_res_dia:
            dias_risco_caixa.append(item_dia)
        if concentracao_alta:
            dias_concentracao_pagto.append(item_dia)

    grafico_fluxo_diario_html = ""
    if gerar_graficos and fluxo_diario:
        df_fd = pd.DataFrame(fluxo_diario)
        fig_fd = go.Figure()
        fig_fd.add_trace(
            go.Bar(
                x=df_fd["data_str"],
                y=df_fd["entradas_raw"],
                name="Entradas (R$)",
                marker_color="#16a34a",
            )
        )
        fig_fd.add_trace(
            go.Bar(
                x=df_fd["data_str"],
                y=df_fd["saidas_raw"],
                name="Saídas (R$)",
                marker_color="#dc2626",
            )
        )
        fig_fd.add_trace(
            go.Scatter(
                x=df_fd["data_str"],
                y=df_fd["saldo_projetado_raw"],
                name="Saldo Projetado (R$)",
                mode="lines+markers",
                line=dict(color="#0284c7", width=3),
                marker=dict(size=6),
            )
        )
        fig_fd.add_hline(
            y=reserva_minima,
            line_dash="dash",
            line_color="#b91c1c",
            annotation_text=f"Reserva Mínima ({fmt_brl(reserva_minima)})",
            annotation_position="top left",
        )
        fig_fd.update_layout(
            barmode="group",
            title=f"📅 Fluxo de Caixa Diário & Saldo Projetado — {MESES_NOMES_PT[mes_selecionado]}/{ano_selecionado}",
            xaxis_title="Dia do Mês",
            yaxis_title="Valor (R$)",
            height=350,
            margin=dict(t=45, b=30, l=25, r=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        grafico_fluxo_diario_html = fig_to_html(fig_fd)

    # -------------------------------------------------------------------------
    # 5. RESUMO DE CONTAS A RECEBER, ENVELHECIMENTO E INADIMPLÊNCIA (SEÇÕES 12, 13, 14)
    # -------------------------------------------------------------------------
    val_a_vencer = 0.0
    qtd_a_vencer = 0
    val_vencido_1_30 = 0.0
    qtd_vencido_1_30 = 0
    val_vencido_mais_30 = 0.0
    qtd_vencido_mais_30 = 0
    lista_cobranca_inadimplencia = []

    for r in cr_operacional:
        st = r["status"]
        if st == "RECEBIDO":
            continue
        dt_v = r["data_vencimento"]
        v_tot = float(r["valor_total"] or 0.0)
        v_sal = float(r["valor_saldo"] or 0.0)
        saldo_aberto = v_sal if v_sal > 0 else v_tot
        if saldo_aberto <= 0:
            continue

        dias_atr = int(r["dias_atraso"] or 0)
        if dt_v and dt_v < hoje and dias_atr <= 0:
            dias_atr = (hoje - dt_v).days

        eh_vencido = (st == "VENCIDO") or (dt_v is not None and dt_v < hoje)
        if not eh_vencido:
            val_a_vencer += saldo_aberto
            qtd_a_vencer += 1
        else:
            dias_atr = max(dias_atr, 1)
            if dias_atr <= 30:
                val_vencido_1_30 += saldo_aberto
                qtd_vencido_1_30 += 1
                faixa_atraso = "1 a 30 dias"
            else:
                val_vencido_mais_30 += saldo_aberto
                qtd_vencido_mais_30 += 1
                faixa_atraso = "+30 dias"

            lista_cobranca_inadimplencia.append(
                {
                    "id": r["id"],
                    "cliente_nome": r["cliente_nome"] or "CLIENTE NÃO IDENTIFICADO",
                    "cliente_documento": r["cliente_documento"] or "-",
                    "vendedor_nome": r["vendedor_nome"] or "-",
                    "empresa": r["empresa"],
                    "documento": r["numero_duplicata"] or r["numero_documento"] or r["id_titulo_erp"],
                    "parcela": r["parcela"] or "-",
                    "portador": (r["portador"] or "NÃO INFORMADO").strip().upper(),
                    "data_venc_str": dt_v.strftime("%d/%m/%Y") if dt_v else "-",
                    "data_venc_iso": dt_v.strftime("%Y-%m-%d") if dt_v else "",
                    "dias_atraso": dias_atr,
                    "faixa_atraso": faixa_atraso,
                    "valor_aberto": saldo_aberto,
                    "valor_aberto_fmt": fmt_brl(saldo_aberto),
                }
            )

    lista_cobranca_inadimplencia.sort(key=lambda x: (x["valor_aberto"], x["dias_atraso"]), reverse=True)

    total_vencido_cr = val_vencido_1_30 + val_vencido_mais_30
    qtd_total_vencido_cr = qtd_vencido_1_30 + qtd_vencido_mais_30
    total_carteira_cr = val_a_vencer + total_vencido_cr
    qtd_total_carteira_cr = qtd_a_vencer + qtd_total_vencido_cr

    taxa_inadimplencia_valor_pct = (total_vencido_cr / total_carteira_cr * 100.0) if total_carteira_cr > 0 else 0.0
    taxa_inadimplencia_qtd_pct = (qtd_total_vencido_cr / qtd_total_carteira_cr * 100.0) if qtd_total_carteira_cr > 0 else 0.0

    if qtd_total_vencido_cr > 0:
        if abs(taxa_inadimplencia_qtd_pct - taxa_inadimplencia_valor_pct) >= 3.0:
            if taxa_inadimplencia_qtd_pct > taxa_inadimplencia_valor_pct:
                frase_inadimplencia = (
                    f"{fmt_pct(taxa_inadimplencia_qtd_pct)} dos títulos em aberto estão vencidos ({qtd_total_vencido_cr} de {qtd_total_carteira_cr}), "
                    f"mas representam {fmt_pct(taxa_inadimplencia_valor_pct)} do valor financeiro total a receber."
                )
            else:
                frase_inadimplencia = (
                    f"Apenas {fmt_pct(taxa_inadimplencia_qtd_pct)} dos títulos estão vencidos ({qtd_total_vencido_cr} títulos), "
                    f"porém concentram {fmt_pct(taxa_inadimplencia_valor_pct)} de todo o valor financeiro a receber."
                )
        else:
            frase_inadimplencia = (
                f"{fmt_pct(taxa_inadimplencia_qtd_pct)} dos títulos em aberto estão vencidos ({qtd_total_vencido_cr} títulos), "
                f"representando {fmt_pct(taxa_inadimplencia_valor_pct)} do valor total a receber."
            )
    else:
        frase_inadimplencia = "Não há títulos vencidos em aberto na carteira filtrada."

    resumo_inadimplencia = {
        "a_vencer": fmt_brl(val_a_vencer),
        "a_vencer_raw": val_a_vencer,
        "qtd_a_vencer": qtd_a_vencer,
        "vencido_1_30": fmt_brl(val_vencido_1_30),
        "vencido_1_30_raw": val_vencido_1_30,
        "qtd_vencido_1_30": qtd_vencido_1_30,
        "vencido_mais_30": fmt_brl(val_vencido_mais_30),
        "vencido_mais_30_raw": val_vencido_mais_30,
        "qtd_vencido_mais_30": qtd_vencido_mais_30,
        "total_em_atraso": fmt_brl(total_vencido_cr),
        "total_em_atraso_raw": total_vencido_cr,
        "qtd_em_atraso": qtd_total_vencido_cr,
        "total_contas_receber": fmt_brl(total_carteira_cr),
        "total_contas_receber_raw": total_carteira_cr,
        "qtd_total_contas_receber": qtd_total_carteira_cr,
        "taxa_inadimplencia_valor": fmt_pct(taxa_inadimplencia_valor_pct),
        "taxa_inadimplencia_valor_raw": taxa_inadimplencia_valor_pct,
        "taxa_inadimplencia_qtd": fmt_pct(taxa_inadimplencia_qtd_pct),
        "taxa_inadimplencia_qtd_raw": taxa_inadimplencia_qtd_pct,
        "frase_analitica": frase_inadimplencia,
        "lista_cobranca": lista_cobranca_inadimplencia,
    }

    # -------------------------------------------------------------------------
    # 6. ALERTAS FINANCEIROS & REGRA PRINCIPAL DO DASHBOARD (SEÇÕES 5, 6, 14, 18, 20)
    # -------------------------------------------------------------------------
    alertas_financeiros = []
    acoes_sugeridas_caixa = [
        "Aumentar ou antecipar recebimentos do período;",
        "Realizar cobrança ativa dos clientes com títulos vencidos;",
        "Avaliar antecipação pontual de recebíveis, se aplicável;",
        "Restringir novas compras com vencimento no período crítico;",
        "Negociar ou postergar pagamentos possíveis junto a fornecedores;",
        "Revisar e cortar despesas previstas não essenciais.",
    ]

    for m_crit in meses_abaixo_reserva:
        alertas_financeiros.append(
            {
                "nivel": "PERIGO",
                "icone": "🔴",
                "titulo": f"ATENÇÃO: Caixa projetado abaixo da reserva mínima em {m_crit['mes_ano_str']}.",
                "detalhe": (
                    f"Saldo projetado: {m_crit['saldo_projetado']} | "
                    f"Reserva mínima: {m_crit['reserva_minima']} | "
                    f"Necessidade adicional: {m_crit['necessidade_adicional']}"
                ),
            }
        )

    if dias_risco_caixa:
        primeiro_dia_risco = dias_risco_caixa[0]
        pior_dia = min(dias_risco_caixa, key=lambda d: d["saldo_projetado_raw"])
        alertas_financeiros.append(
            {
                "nivel": "PERIGO",
                "icone": "🔴",
                "titulo": f"Risco de caixa em {primeiro_dia_risco['data_str']} — saldo projetado abaixo da reserva mínima.",
                "detalhe": (
                    f"Em {len(dias_risco_caixa)} dia(s) de {MESES_NOMES_PT[mes_selecionado]}/{ano_selecionado} o caixa fica abaixo de {fmt_brl(reserva_minima)} "
                    f"(menor saldo em {pior_dia['data_str']}: {pior_dia['saldo_projetado']})."
                ),
            }
        )

    if dias_concentracao_pagto:
        maior_dia_pag = max(dias_concentracao_pagto, key=lambda d: d["saidas_raw"])
        alertas_financeiros.append(
            {
                "nivel": "ATENCAO",
                "icone": "🟠",
                "titulo": f"Alta concentração de pagamentos em {maior_dia_pag['data_str']} ({maior_dia_pag['saidas']}).",
                "detalhe": (
                    f"O dia {maior_dia_pag['data_str']} concentra forte saída de caixa. "
                    f"Saldo projetado no dia: {maior_dia_pag['saldo_projetado']}."
                ),
            }
        )

    if total_vencido_cr > 0:
        alertas_financeiros.append(
            {
                "nivel": "ATENCAO",
                "icone": "🟠",
                "titulo": f"Atenção: {fmt_brl(total_vencido_cr)} em contas vencidas ({qtd_total_vencido_cr} títulos).",
                "detalhe": (
                    f"Vencido 1–30 dias: {fmt_brl(val_vencido_1_30)} ({qtd_vencido_1_30}) | "
                    f"Vencido +30 dias: {fmt_brl(val_vencido_mais_30)} ({qtd_vencido_mais_30}) | "
                    f"Taxa de inadimplência: {fmt_pct(taxa_inadimplencia_valor_pct)}."
                ),
                "acao_modal": "#modalListaCobranca",
            }
        )

    primeiro_mes_proj = projecao_mensal[0] if projecao_mensal else {
        "recebimentos_previstos": fmt_brl(0),
        "recebimentos_previstos_raw": 0.0,
        "qtd_recebimentos": 0,
        "pagamentos_previstos": fmt_brl(0),
        "pagamentos_previstos_raw": 0.0,
        "qtd_pagamentos": 0,
        "saldo_projetado": fmt_brl(saldo_bancario_informado),
        "saldo_projetado_raw": saldo_bancario_informado,
        "disponivel": fmt_brl(saldo_bancario_informado - reserva_minima),
        "disponivel_raw": saldo_bancario_informado - reserva_minima,
        "abaixo_reserva": (saldo_bancario_informado < reserva_minima),
        "mes_ano_str": f"{MESES_NOMES_PT[mes_selecionado]}/{ano_selecionado}",
    }

    if primeiro_mes_proj["disponivel_raw"] > 0:
        alertas_financeiros.append(
            {
                "nivel": "SUCESSO",
                "icone": "🟢",
                "titulo": f"{primeiro_mes_proj['disponivel']} disponíveis para novos compromissos em {primeiro_mes_proj['mes_ano_str']}.",
                "detalhe": (
                    f"Saldo projetado ({primeiro_mes_proj['saldo_projetado']}) acima da reserva mínima "
                    f"operacional de {fmt_brl(reserva_minima)}."
                ),
            }
        )

    # Semáforo Geral (Seção 20)
    algum_abaixo = bool(meses_abaixo_reserva or dias_risco_caixa)
    algum_atencao = bool(
        any(p["proximo_reserva"] for p in projecao_mensal)
        or dias_concentracao_pagto
        or taxa_inadimplencia_valor_pct >= 10.0
    )

    if algum_abaixo:
        status_geral = {
            "codigo": "ACAO_NECESSARIA",
            "icone": "🔴",
            "titulo": "AÇÃO NECESSÁRIA — CAIXA ABAIXO DA RESERVA MÍNIMA",
            "classe_card": "border-danger bg-danger-subtle text-danger-emphasis",
            "mensagem": "É necessário aumentar os recebimentos e/ou restringir novos compromissos com vencimento neste período.",
            "exibir_acoes": True,
            "acoes_sugeridas": acoes_sugeridas_caixa,
        }
    elif algum_atencao:
        status_geral = {
            "codigo": "ATENCAO",
            "icone": "🟡",
            "titulo": "ATENÇÃO — MONITORAMENTO DE LIQUIDEZ E CONCENTRAÇÃO",
            "classe_card": "border-warning bg-warning-subtle text-dark",
            "mensagem": "Caixa projetado próximo da reserva mínima, concentração relevante de pagamentos ou inadimplência exigindo acompanhamento.",
            "exibir_acoes": False,
            "acoes_sugeridas": acoes_sugeridas_caixa,
        }
    else:
        status_geral = {
            "codigo": "SAUDAVEL",
            "icone": "🟢",
            "titulo": "SITUAÇÃO SAUDÁVEL",
            "classe_card": "border-success bg-success-subtle text-success-emphasis",
            "mensagem": f"Caixa projetado acima de {fmt_brl(reserva_minima)} em todo o período analisado e existência de valor disponível para assumir novos compromissos.",
            "exibir_acoes": False,
            "acoes_sugeridas": [],
        }

    # Cards de Topo (Seção 18)
    topo_liquidez = {
        "saldo_bancario_informado": fmt_brl(saldo_bancario_informado),
        "saldo_bancario_informado_raw": saldo_bancario_informado,
        "data_atualizacao_saldo": timezone.localtime(cfg.data_atualizacao).strftime("%d/%m/%Y às %H:%M") if cfg.data_atualizacao else "-",
        "usuario_atualizacao_saldo": cfg.usuario_atualizacao or "Sistema",
        "contas_a_receber": primeiro_mes_proj["recebimentos_previstos"],
        "contas_a_receber_raw": primeiro_mes_proj["recebimentos_previstos_raw"],
        "qtd_a_receber": primeiro_mes_proj["qtd_recebimentos"],
        "contas_a_pagar": primeiro_mes_proj["pagamentos_previstos"],
        "contas_a_pagar_raw": primeiro_mes_proj["pagamentos_previstos_raw"],
        "qtd_a_pagar": primeiro_mes_proj["qtd_pagamentos"],
        "saldo_projetado": primeiro_mes_proj["saldo_projetado"],
        "saldo_projetado_raw": primeiro_mes_proj["saldo_projetado_raw"],
        "reserva_minima": fmt_brl(reserva_minima),
        "reserva_minima_raw": reserva_minima,
        "disponivel": primeiro_mes_proj["disponivel"],
        "disponivel_raw": primeiro_mes_proj["disponivel_raw"],
        "disponivel_positivo": primeiro_mes_proj["disponivel_raw"] >= 0,
        "mes_referencia_str": primeiro_mes_proj["mes_ano_str"],
    }

    resumo_intercompany = {
        "ativo": consolidado,
        "qtd_cr": qtd_transf_cr,
        "val_cr": fmt_brl(val_transf_cr),
        "qtd_cp": qtd_transf_cp,
        "val_cp": fmt_brl(val_transf_cp),
        "total_neutralizado": fmt_brl(val_transf_cr + val_transf_cp),
        "tem_transferencias": (qtd_transf_cr + qtd_transf_cp) > 0,
    }

    return {
        "cfg": cfg,
        "topo_liquidez": topo_liquidez,
        "status_geral": status_geral,
        "projecao_mensal": projecao_mensal,
        "meses_abaixo_reserva": meses_abaixo_reserva,
        "resultado_simulacao": resultado_simulacao,
        "detalhe_cr_mes": detalhe_cr_mes,
        "detalhe_cp_mes": detalhe_cp_mes,
        "diagnostico_titulos_cr": diagnostico_titulos_cr,
        "fluxo_diario": fluxo_diario,
        "grafico_fluxo_diario": grafico_fluxo_diario_html,
        "dias_risco_caixa": dias_risco_caixa,
        "menor_saldo_dia": menor_saldo_dia,
        "resumo_inadimplencia": resumo_inadimplencia,
        "alertas_financeiros": alertas_financeiros,
        "resumo_intercompany": resumo_intercompany,
    }


def gerar_modulo_gestao_custos(
    mes_selecionado: int,
    ano_selecionado: int,
    empresa_filtro: str = "TODAS",
    gerar_graficos: bool = True,
) -> dict:
    """
    Módulo completo de GESTÃO E CONTROLE DE CUSTOS | AMM EPIs (Seções 1 a 24 do Prompt de Custos):
    - Classificação de todas as despesas em:
      A. CMV | B. Custos Fixos | C. Custos Variáveis | D. Despesas Financeiras | E. Extraordinárias
    - Comparativos: Mês Atual vs Mês Anterior, Média 3M, Média 6M, Acumulado do Ano (YTD) e Mesmo Mês Ano Anterior (YoY)
    - Indicadores dedicados: Custo Fixo, Custo Variável, CMV, Comissões + Bonificações, Transporte (Combustível + Fretes), Custo Total, Lucro Líquido
    - Top 10 Maiores Custos & Custo como % do Faturamento
    - O Que Mudou? (Aumentaram vs Diminuíram)
    - Alertas Inteligentes de Custos (com filtro de média histórica de 6M e detecção de desvios fora do padrão)
    - Custos Fixos Recorrentes & Drill-down de Custo Fixo por Categoria ("Por que nosso custo fixo aumentou?")
    - Gráfico "Para onde está indo cada R$ 1,00 vendido?", Evolução do Lucro Líquido e Tabela Vendas vs Custos.
    """
    cfg = obter_configuracao_financeira(empresa_filtro)
    limite_alerta_pct = float(cfg.alerta_aumento_pct or 10.0)
    limite_alerta_val = float(cfg.alerta_aumento_valor or 500.0)

    mapa_classificacao = sincronizar_mapa_classificacao_custos()

    # Coordenadas temporais
    ym_atual = (ano_selecionado, mes_selecionado)
    ym_ant = _adicionar_meses(ano_selecionado, mes_selecionado, -1)
    ultimos_3m = [_adicionar_meses(ano_selecionado, mes_selecionado, -i) for i in range(1, 4)]
    ultimos_6m = [_adicionar_meses(ano_selecionado, mes_selecionado, -i) for i in range(1, 7)]
    serie_6m_completa = [_adicionar_meses(ano_selecionado, mes_selecionado, -i) for i in range(5, -1, -1)]
    ym_yoy = (ano_selecionado - 1, mes_selecionado)

    # Faturamento mensal (NFs emitidas no mês; fallback para Contas a Receber se NF=0 em mês futuro)
    nf_qs = NotaFiscal.objects.exclude(status="CANCELADA")
    cr_qs = ContaReceber.objects.filter(cancelada=False).exclude(status="CANCELADO")
    cp_qs = ContaPagar.objects.filter(cancelada=False).exclude(status="CANCELADO")

    if empresa_filtro and empresa_filtro != "TODAS":
        nf_qs = nf_qs.filter(empresa=empresa_filtro)
        cr_qs = cr_qs.filter(empresa=empresa_filtro)
        cp_qs = cp_qs.filter(empresa=empresa_filtro)

    faturamento_nf_por_ym = defaultdict(float)
    for dt_em, val in nf_qs.values_list("data_emissao", "valor_total"):
        if dt_em:
            faturamento_nf_por_ym[(dt_em.year, dt_em.month)] += float(val or 0.0)

    receita_cr_por_ym = defaultdict(float)
    for dt_v, val in cr_qs.values_list("data_vencimento", "valor_total"):
        if dt_v:
            receita_cr_por_ym[(dt_v.year, dt_v.month)] += float(val or 0.0)

    def _obter_faturamento_ym(ym: tuple[int, int]) -> float:
        fat_nf = faturamento_nf_por_ym.get(ym, 0.0)
        if fat_nf > 0:
            return fat_nf
        return receita_cr_por_ym.get(ym, 0.0)

    # Carrega Contas a Pagar
    cp_rows = list(
        cp_qs.values(
            "id",
            "id_titulo_erp",
            "empresa",
            "numero_duplicata",
            "numero_documento",
            "fornecedor_nome",
            "fornecedor_documento",
            "data_vencimento",
            "data_pagamento",
            "valor_total",
            "valor_pago",
            "centro_custo",
            "grupo_conta",
            "subconta",
            "observacao",
            "status",
        )
    )

    consolidado = (not empresa_filtro) or empresa_filtro == "TODAS"

    # Estruturas de agregação por (ano, mes)
    # tipo_por_ym[ym][tipo_custo], cat_por_ym[ym][categoria], conta_por_ym[ym][conta_chave]
    tipo_por_ym = defaultdict(lambda: defaultdict(float))
    cat_por_ym = defaultdict(lambda: defaultdict(float))
    conta_por_ym = defaultdict(lambda: defaultdict(float))
    cat_fixo_por_ym = defaultdict(lambda: defaultdict(float))
    detalhes_fixo_mes_atual = defaultdict(list)

    # Indicadores especiais por ym
    comissao_por_ym = defaultdict(float)
    bonificacao_por_ym = defaultdict(float)
    combustivel_por_ym = defaultdict(float)
    fretes_por_ym = defaultdict(float)

    for p in cp_rows:
        dt_v = p["data_vencimento"]
        if not dt_v:
            continue
        if consolidado and eh_transferencia_intercompany(
            nome_parte=p.get("fornecedor_nome"),
            documento_parte=p.get("fornecedor_documento"),
            grupo_conta=p.get("grupo_conta"),
            subconta=p.get("subconta"),
            observacao=p.get("observacao"),
        ):
            continue

        ym = (dt_v.year, dt_v.month)
        val = float(p["valor_total"] or 0.0)
        if val <= 0:
            continue

        cc = (p["centro_custo"] or "").strip().upper()
        gc = (p["grupo_conta"] or "").strip().upper()
        sc = (p["subconta"] or "").strip().upper()
        chave = (sc or gc or cc or "OUTROS").strip().upper()

        info_cls = mapa_classificacao.get(chave)
        if not info_cls:
            pad = inferir_classificacao_padrao(cc, gc, sc)
            info_cls = {
                "conta_chave": chave,
                "categoria": pad["categoria"],
                "subcategoria": pad["subcategoria"],
                "tipo_custo": pad["tipo_custo"],
                "recorrente": pad["recorrente"],
            }

        t_custo = info_cls["tipo_custo"]
        cat = info_cls["categoria"]

        tipo_por_ym[ym][t_custo] += val
        cat_por_ym[ym][cat] += val
        conta_por_ym[ym][chave] += val

        if t_custo == "FIXO":
            cat_fixo_por_ym[ym][cat] += val
            if ym == ym_atual:
                detalhes_fixo_mes_atual[cat].append(
                    {
                        "conta": chave,
                        "subcategoria": info_cls.get("subcategoria", "-"),
                        "fornecedor": p["fornecedor_nome"] or "-",
                        "documento": p["numero_duplicata"] or p["numero_documento"] or p["id_titulo_erp"],
                        "vencimento": dt_v.strftime("%d/%m/%Y"),
                        "valor": val,
                        "valor_fmt": fmt_brl(val),
                        "status": p["status"],
                    }
                )

        # Indicadores Específicos: Comissões, Bonificações, Combustível e Fretes
        texto_c = f"{cat.upper()} | {chave}"
        if cat == "Comissões" or "COMISS" in chave:
            comissao_por_ym[ym] += val
        elif cat == "Bonificações" or "BONIFICA" in chave or "BONUS" in chave:
            bonificacao_por_ym[ym] += val

        if "COMBUST" in chave or "GASOLINA" in texto_c:
            combustivel_por_ym[ym] += val
        elif (cat == "Fretes / Transportadoras" or "FRETE" in chave) and t_custo != "CMV":
            fretes_por_ym[ym] += val

    # Função auxiliar para calcular métricas comparativas de qualquer série mensal
    def _calcular_comparativo_serie(fn_valor_ym, fat_atual_ref: float, fat_ant_ref: float) -> dict:
        v_atual = float(fn_valor_ym(ym_atual))
        v_ant = float(fn_valor_ym(ym_ant))
        vals_3m = [float(fn_valor_ym(m)) for m in ultimos_3m if float(fn_valor_ym(m)) > 0 or _obter_faturamento_ym(m) > 0]
        vals_6m = [float(fn_valor_ym(m)) for m in ultimos_6m if float(fn_valor_ym(m)) > 0 or _obter_faturamento_ym(m) > 0]
        media_3m = float(np.mean(vals_3m)) if vals_3m else 0.0
        media_6m = float(np.mean(vals_6m)) if vals_6m else 0.0

        ytd_meses = [(ano_selecionado, m) for m in range(1, mes_selecionado + 1)]
        acum_ano = sum(float(fn_valor_ym(m)) for m in ytd_meses)
        v_yoy = float(fn_valor_ym(ym_yoy))

        var_rs = v_atual - v_ant
        var_pct = ((v_atual - v_ant) / v_ant * 100.0) if v_ant > 0 else 0.0
        var_3m_pct = ((v_atual - media_3m) / media_3m * 100.0) if media_3m > 0 else 0.0
        var_6m_pct = ((v_atual - media_6m) / media_6m * 100.0) if media_6m > 0 else 0.0
        pct_fat_atual = (v_atual / fat_atual_ref * 100.0) if fat_atual_ref > 0 else 0.0
        pct_fat_ant = (v_ant / fat_ant_ref * 100.0) if fat_ant_ref > 0 else 0.0

        return {
            "atual_raw": v_atual,
            "atual": fmt_brl(v_atual),
            "anterior_raw": v_ant,
            "anterior": fmt_brl(v_ant),
            "media_3m_raw": media_3m,
            "media_3m": fmt_brl(media_3m),
            "media_6m_raw": media_6m,
            "media_6m": fmt_brl(media_6m),
            "acumulado_ano_raw": acum_ano,
            "acumulado_ano": fmt_brl(acum_ano),
            "yoy_raw": v_yoy,
            "yoy": fmt_brl(v_yoy) if v_yoy > 0 else "Sem base",
            "var_rs_raw": var_rs,
            "var_rs": fmt_brl(var_rs),
            "var_pct_raw": var_pct,
            "var_pct": fmt_pct(var_pct, com_sinal=True),
            "var_3m_pct_raw": var_3m_pct,
            "var_3m_pct": fmt_pct(var_3m_pct, com_sinal=True),
            "var_6m_pct_raw": var_6m_pct,
            "var_6m_pct": fmt_pct(var_6m_pct, com_sinal=True),
            "pct_faturamento_raw": pct_fat_atual,
            "pct_faturamento": fmt_pct(pct_fat_atual, casas=2),
            "pct_faturamento_ant_raw": pct_fat_ant,
            "pct_faturamento_ant": fmt_pct(pct_fat_ant, casas=2),
            "aumentou": var_rs > 0,
        }

    fat_atual = _obter_faturamento_ym(ym_atual)
    fat_ant = _obter_faturamento_ym(ym_ant)
    fat_var_rs = fat_atual - fat_ant
    fat_var_pct = ((fat_atual - fat_ant) / fat_ant * 100.0) if fat_ant > 0 else 0.0

    # 1. Indicadores Principais (Seções 3, 4, 5, 6, 7, 12, 13)
    ind_cmv = _calcular_comparativo_serie(lambda ym: tipo_por_ym[ym]["CMV"], fat_atual, fat_ant)
    ind_fixo = _calcular_comparativo_serie(lambda ym: tipo_por_ym[ym]["FIXO"], fat_atual, fat_ant)
    ind_variavel = _calcular_comparativo_serie(lambda ym: tipo_por_ym[ym]["VARIAVEL"], fat_atual, fat_ant)
    ind_financeiro = _calcular_comparativo_serie(lambda ym: tipo_por_ym[ym]["FINANCEIRO"], fat_atual, fat_ant)
    ind_extraordinario = _calcular_comparativo_serie(lambda ym: tipo_por_ym[ym]["EXTRAORDINARIO"], fat_atual, fat_ant)

    def _custo_total_operacional_ym(ym):
        t = tipo_por_ym[ym]
        return t["CMV"] + t["FIXO"] + t["VARIAVEL"] + t["FINANCEIRO"]

    ind_custo_total = _calcular_comparativo_serie(_custo_total_operacional_ym, fat_atual, fat_ant)

    # Margem Bruta (Seção 5)
    margem_bruta_atual = fat_atual - ind_cmv["atual_raw"]
    margem_bruta_pct = (margem_bruta_atual / fat_atual * 100.0) if fat_atual > 0 else 0.0
    ind_cmv["margem_bruta"] = fmt_brl(margem_bruta_atual)
    ind_cmv["margem_bruta_pct"] = fmt_pct(margem_bruta_pct)
    ind_cmv["pressionando_margem"] = (ind_cmv["var_pct_raw"] > fat_var_pct + 1.0) and (ind_cmv["var_rs_raw"] > 0)

    # Comissões + Bonificações (Seção 6)
    ind_comissao_sep = _calcular_comparativo_serie(lambda ym: comissao_por_ym[ym], fat_atual, fat_ant)
    ind_bonificacao_sep = _calcular_comparativo_serie(lambda ym: bonificacao_por_ym[ym], fat_atual, fat_ant)
    ind_comissoes_bonif = _calcular_comparativo_serie(
        lambda ym: comissao_por_ym[ym] + bonificacao_por_ym[ym], fat_atual, fat_ant
    )
    ind_comissoes_bonif["comissao_separada"] = ind_comissao_sep
    ind_comissoes_bonif["bonificacao_separada"] = ind_bonificacao_sep
    ind_comissoes_bonif["desproporcional"] = (
        ind_comissoes_bonif["var_pct_raw"] > (fat_var_pct + 3.0) and ind_comissoes_bonif["var_rs_raw"] > 500
    )

    # Transporte: Gasolina / Combustível + Transportadoras / Fretes (Seção 7)
    ind_combustivel_sep = _calcular_comparativo_serie(lambda ym: combustivel_por_ym[ym], fat_atual, fat_ant)
    ind_fretes_sep = _calcular_comparativo_serie(lambda ym: fretes_por_ym[ym], fat_atual, fat_ant)
    ind_transporte = _calcular_comparativo_serie(
        lambda ym: combustivel_por_ym[ym] + fretes_por_ym[ym], fat_atual, fat_ant
    )
    ind_transporte["combustivel_separado"] = ind_combustivel_sep
    ind_transporte["fretes_separado"] = ind_fretes_sep

    # Lucro Líquido & Margem Líquida (Seção 13 e 16)
    lucro_atual = fat_atual - ind_custo_total["atual_raw"]
    lucro_ant = fat_ant - ind_custo_total["anterior_raw"]
    margem_liq_atual = (lucro_atual / fat_atual * 100.0) if fat_atual > 0 else 0.0
    margem_liq_ant = (lucro_ant / fat_ant * 100.0) if fat_ant > 0 else 0.0
    lucro_var_rs = lucro_atual - lucro_ant
    lucro_var_pct = ((lucro_atual - lucro_ant) / abs(lucro_ant) * 100.0) if lucro_ant != 0 else 0.0

    # Diagnóstico de Rentabilidade (Seção 16 — Situações 1, 2 e 3)
    if fat_var_pct >= 0 and lucro_var_pct >= fat_var_pct and margem_liq_atual >= margem_liq_ant:
        diagnostico_rentabilidade = {
            "icone": "🟢",
            "classe": "success",
            "titulo": "Rentabilidade em evolução.",
            "justificativa": (
                f"Faturamento variou {fmt_pct(fat_var_pct, com_sinal=True)}, lucro variou {fmt_pct(lucro_var_pct, com_sinal=True)} "
                f"e a margem líquida evoluiu de {fmt_pct(margem_liq_ant)} para {fmt_pct(margem_liq_atual)}."
            ),
        }
    elif fat_var_pct > 0 and margem_liq_atual < margem_liq_ant and lucro_atual >= lucro_ant:
        diagnostico_rentabilidade = {
            "icone": "🟠",
            "classe": "warning",
            "titulo": "Faturamento cresceu, mas a rentabilidade está sendo pressionada.",
            "justificativa": (
                f"Faturamento cresceu {fmt_pct(fat_var_pct, com_sinal=True)}, mas os custos subiram {ind_custo_total['var_pct']}, "
                f"reduzindo a margem líquida de {fmt_pct(margem_liq_ant)} para {fmt_pct(margem_liq_atual)}."
            ),
        }
    else:
        diagnostico_rentabilidade = {
            "icone": "🔴",
            "classe": "danger",
            "titulo": "Custos estão crescendo acima das vendas e pressionando o resultado.",
            "justificativa": (
                f"Vendas variaram {fmt_pct(fat_var_pct, com_sinal=True)} ({fmt_brl(fat_var_rs)}), enquanto os custos "
                f"variaram {ind_custo_total['var_pct']} ({ind_custo_total['var_rs']}). "
                f"Margem líquida passou de {fmt_pct(margem_liq_ant)} para {fmt_pct(margem_liq_atual)}."
            ),
        }

    ind_lucro_liquido = {
        "atual_raw": lucro_atual,
        "atual": fmt_brl(lucro_atual),
        "anterior_raw": lucro_ant,
        "anterior": fmt_brl(lucro_ant),
        "margem_atual_raw": margem_liq_atual,
        "margem_atual": fmt_pct(margem_liq_atual),
        "margem_anterior_raw": margem_liq_ant,
        "margem_anterior": fmt_pct(margem_liq_ant),
        "var_rs_raw": lucro_var_rs,
        "var_rs": fmt_brl(lucro_var_rs),
        "var_pct_raw": lucro_var_pct,
        "var_pct": fmt_pct(lucro_var_pct, com_sinal=True),
        "positivo": lucro_atual >= 0,
        "diagnostico": diagnostico_rentabilidade,
    }

    # -------------------------------------------------------------------------
    # 2. RANKING DE CATEGORIAS (TOP 10 MAIORES CUSTOS - SEÇÕES 8 e 11) & O QUE MUDOU? (SEÇÃO 9)
    # -------------------------------------------------------------------------
    todas_categorias = sorted(
        set(cat_por_ym[ym_atual].keys())
        | set(cat_por_ym[ym_ant].keys())
        | {c for m in ultimos_6m for c in cat_por_ym[m].keys()}
    )

    ranking_categorias = []
    for cat in todas_categorias:
        comp_cat = _calcular_comparativo_serie(lambda ym, c=cat: cat_por_ym[ym][c], fat_atual, fat_ant)
        if comp_cat["atual_raw"] == 0 and comp_cat["anterior_raw"] == 0 and comp_cat["media_6m_raw"] == 0:
            continue
        comp_cat["categoria"] = cat
        ranking_categorias.append(comp_cat)

    ranking_categorias.sort(key=lambda x: x["atual_raw"], reverse=True)
    top_10_custos = ranking_categorias[:10]

    # Seção 9: O QUE MUDOU? (Aumentaram vs Diminuíram, ordenados pela maior variação absoluta em R$)
    aumentaram = [c for c in ranking_categorias if c["var_rs_raw"] > 0]
    aumentaram.sort(key=lambda x: x["var_rs_raw"], reverse=True)

    diminuiram = [c for c in ranking_categorias if c["var_rs_raw"] < 0]
    diminuiram.sort(key=lambda x: abs(x["var_rs_raw"]), reverse=True)

    # -------------------------------------------------------------------------
    # 3. ALERTAS INTELIGENTES DE CUSTOS & DESVIOS FORA DO PADRÃO (SEÇÕES 10, 17, 18, 21)
    # -------------------------------------------------------------------------
    alertas_custos = []
    desvios_historico = []

    # Regra 10.2: Aumento de custo maior que crescimento das vendas
    if ind_custo_total["var_pct_raw"] > (fat_var_pct + 2.0) and ind_custo_total["var_rs_raw"] > 0:
        causadores = ", ".join(
            f"{c['categoria']} (+{c['var_rs']})" for c in aumentaram[:3]
        )
        alertas_custos.append(
            {
                "nivel": "PERIGO",
                "icone": "🔴",
                "titulo": "Os custos estão crescendo mais rapidamente que o faturamento.",
                "detalhe": (
                    f"Faturamento: {fmt_pct(fat_var_pct, com_sinal=True)} | "
                    f"Custos Totais: {ind_custo_total['var_pct']} ({ind_custo_total['var_rs']}). "
                    f"Principais causas: {causadores}."
                ),
            }
        )

    # Regra 5: CMV pressionando margem
    if ind_cmv["pressionando_margem"]:
        alertas_custos.append(
            {
                "nivel": "PERIGO",
                "icone": "🔴",
                "titulo": f"Custo de mercadorias aumentou {ind_cmv['var_pct']}, enquanto o faturamento variou {fmt_pct(fat_var_pct, com_sinal=True)}.",
                "detalhe": (
                    f"CMV atual: {ind_cmv['atual']} ({ind_cmv['pct_faturamento']} das vendas) vs "
                    f"Mês anterior: {ind_cmv['anterior']} | Média 3M: {ind_cmv['media_3m']}."
                ),
            }
        )

    # Regra 10.1 + Seção 17 (Comparação com Média Histórica de 6M para evitar falsos alarmes)
    for c in ranking_categorias:
        v_at = c["atual_raw"]
        v_an = c["anterior_raw"]
        med_6m = c["media_6m_raw"]
        var_rs = c["var_rs_raw"]
        var_pct = c["var_pct_raw"]

        if var_pct >= limite_alerta_pct and var_rs >= limite_alerta_val:
            # Seção 17: Se subiu vs mês anterior, mas ainda está abaixo da média dos últimos 6 meses, não gera alerta vermelho falso!
            if med_6m > 0 and v_at <= med_6m:
                alertas_custos.append(
                    {
                        "nivel": "INFO",
                        "icone": "🔵",
                        "titulo": f"Oscilação dentro do padrão histórico — {c['categoria']} (+{c['var_rs']} / {c['var_pct']}).",
                        "detalhe": (
                            f"Subiu em relação ao mês anterior ({c['anterior']} → {c['atual']}), "
                            f"mas permanece abaixo da média dos últimos 6 meses ({c['media_6m']})."
                        ),
                    }
                )
            else:
                alertas_custos.append(
                    {
                        "nivel": "PERIGO",
                        "icone": "🔴",
                        "titulo": f"ATENÇÃO — {c['categoria']} aumentou {c['var_rs']} ({c['var_pct']}) em relação ao mês anterior.",
                        "detalhe": (
                            f"Atual: {c['atual']} ({c['pct_faturamento']} do faturamento) | "
                            f"Mês anterior: {c['anterior']} | Média 6M: {c['media_6m']}."
                        ),
                    }
                )
        elif var_pct <= -limite_alerta_pct and abs(var_rs) >= limite_alerta_val:
            alertas_custos.append(
                {
                    "nivel": "SUCESSO",
                    "icone": "🟢",
                    "titulo": f"{c['categoria']} reduziu {fmt_brl(abs(var_rs))} ({c['var_pct']}).",
                    "detalhe": f"Passou de {c['anterior']} para {c['atual']} (Média 6M: {c['media_6m']}).",
                }
            )

        # Seção 18: Detectar Desvios Fora do Padrão Histórico (6 meses)
        hist_vals = [cat_por_ym[m][c["categoria"]] for m in ultimos_6m if cat_por_ym[m][c["categoria"]] > 0]
        if len(hist_vals) >= 2 and v_at > 0:
            min_h = float(min(hist_vals))
            max_h = float(max(hist_vals))
            med_h = float(np.mean(hist_vals))
            dif_med_rs = v_at - med_h
            dif_med_pct = (dif_med_rs / med_h * 100.0) if med_h > 0 else 0.0
            if v_at > max_h * 1.15 and dif_med_rs >= limite_alerta_val and dif_med_pct >= 20.0:
                desvios_historico.append(
                    {
                        "categoria": c["categoria"],
                        "valor_atual": fmt_brl(v_at),
                        "faixa_historica": f"{fmt_brl(min_h)} a {fmt_brl(max_h)}",
                        "media_historica": fmt_brl(med_h),
                        "diferenca_rs": fmt_brl(dif_med_rs),
                        "diferenca_pct": fmt_pct(dif_med_pct, com_sinal=True),
                    }
                )

    # Seção 21: Alerta de Crescimento do Custo Fixo nos últimos 3 meses
    ym_3m_atras = _adicionar_meses(ano_selecionado, mes_selecionado, -3)
    fixo_base_3m = tipo_por_ym[ym_3m_atras]["FIXO"] or ind_fixo["media_3m_raw"]
    fixo_atual_val = ind_fixo["atual_raw"]
    aumento_fixo_3m_rs = fixo_atual_val - fixo_base_3m
    aumento_fixo_3m_pct = ((aumento_fixo_3m_rs / fixo_base_3m) * 100.0) if fixo_base_3m > 0 else 0.0

    responsaveis_fixo_3m = []
    for cat_f in set(cat_fixo_por_ym[ym_atual].keys()) | set(cat_fixo_por_ym[ym_3m_atras].keys()):
        base_c = cat_fixo_por_ym[ym_3m_atras][cat_f] or float(np.mean([cat_fixo_por_ym[m][cat_f] for m in ultimos_3m]))
        delta_c = cat_fixo_por_ym[ym_atual][cat_f] - base_c
        if delta_c > 0:
            responsaveis_fixo_3m.append({"categoria": cat_f, "aumento_raw": delta_c, "aumento": fmt_brl(delta_c)})
    responsaveis_fixo_3m.sort(key=lambda x: x["aumento_raw"], reverse=True)

    alerta_custo_fixo_3m = {
        "aumentou": aumento_fixo_3m_rs > 0,
        "aumento_pct": fmt_pct(aumento_fixo_3m_pct, com_sinal=True),
        "aumento_pct_raw": aumento_fixo_3m_pct,
        "aumento_acumulado": fmt_brl(aumento_fixo_3m_rs),
        "principais_responsaveis": responsaveis_fixo_3m[:3],
    }

    # -------------------------------------------------------------------------
    # 4. CUSTO FIXO POR CATEGORIA COM DRILL-DOWN (SEÇÃO 20) & CUSTOS RECORRENTES (SEÇÃO 19)
    # -------------------------------------------------------------------------
    custo_fixo_por_categoria = []
    cats_fixas_todas = sorted(set(cat_fixo_por_ym[ym_atual].keys()) | set(cat_fixo_por_ym[ym_ant].keys()))
    for idx_cf, cat_f in enumerate(cats_fixas_todas):
        v_at = cat_fixo_por_ym[ym_atual][cat_f]
        v_an = cat_fixo_por_ym[ym_ant][cat_f]
        if v_at == 0 and v_an == 0:
            continue
        var_rs = v_at - v_an
        var_pct = (var_rs / v_an * 100.0) if v_an > 0 else 0.0
        pct_do_fixo = (v_at / fixo_atual_val * 100.0) if fixo_atual_val > 0 else 0.0
        lancamentos_cat = sorted(detalhes_fixo_mes_atual.get(cat_f, []), key=lambda x: x["valor"], reverse=True)
        custo_fixo_por_categoria.append(
            {
                "slug": f"cf-cat-{idx_cf}",
                "categoria": cat_f,
                "atual_raw": v_at,
                "atual": fmt_brl(v_at),
                "anterior": fmt_brl(v_an),
                "var_rs_raw": var_rs,
                "var_rs": fmt_brl(var_rs),
                "var_pct": fmt_pct(var_pct, com_sinal=True),
                "pct_do_fixo": fmt_pct(pct_do_fixo),
                "qtd_lancamentos": len(lancamentos_cat),
                "lancamentos": lancamentos_cat,
            }
        )
    custo_fixo_por_categoria.sort(key=lambda x: x["atual_raw"], reverse=True)

    # Seção 19: Custos Fixos Recorrentes
    custos_fixos_recorrentes = []
    for chave_c, info_c in mapa_classificacao.items():
        if not info_c["recorrente"] or info_c["tipo_custo"] not in ("FIXO", "FINANCEIRO"):
            continue
        v_at = conta_por_ym[ym_atual][chave_c]
        v_an = conta_por_ym[ym_ant][chave_c]
        vals_h = [conta_por_ym[m][chave_c] for m in ultimos_6m if conta_por_ym[m][chave_c] > 0]
        med_h = float(np.mean(vals_h)) if vals_h else v_an
        if v_at == 0 and v_an == 0 and med_h == 0:
            continue
        base_comp = v_an if v_an > 0 else med_h
        var_pct = ((v_at - base_comp) / base_comp * 100.0) if base_comp > 0 else 0.0
        subiu = (v_at > base_comp) and ((v_at - base_comp) >= 100.0) and (var_pct >= 5.0)
        custos_fixos_recorrentes.append(
            {
                "despesa": chave_c,
                "categoria": info_c["categoria"],
                "atual_raw": v_at,
                "atual": fmt_brl(v_at),
                "anterior": fmt_brl(v_an),
                "media": fmt_brl(med_h),
                "variacao_pct": fmt_pct(var_pct, com_sinal=True),
                "variacao_pct_raw": var_pct,
                "alerta_aumento": subiu,
                "mensagem_alerta": (
                    f"O custo mensal de {chave_c} passou de {fmt_brl(base_comp)} para {fmt_brl(v_at)}."
                    if subiu
                    else ""
                ),
            }
        )
    custos_fixos_recorrentes.sort(key=lambda x: x["atual_raw"], reverse=True)

    # -------------------------------------------------------------------------
    # 5. EVOLUÇÃO MENSAL DE LUCRO E RELAÇÃO ENTRE VENDAS E CUSTOS (SEÇÕES 14, 15, 22)
    # -------------------------------------------------------------------------
    evolucao_mensal = []
    fat_prev_loop = None
    custo_prev_loop = None

    for y_s, m_s in serie_6m_completa:
        ym_s = (y_s, m_s)
        vendas_s = _obter_faturamento_ym(ym_s)
        custos_s = _custo_total_operacional_ym(ym_s)
        lucro_s = vendas_s - custos_s
        margem_s = (lucro_s / vendas_s * 100.0) if vendas_s > 0 else 0.0

        diagnostico_rel = "Base inicial da série"
        classe_rel = "secondary"
        if fat_prev_loop is not None and fat_prev_loop > 0 and custo_prev_loop is not None and custo_prev_loop > 0:
            dv = (vendas_s - fat_prev_loop) / fat_prev_loop * 100.0
            dc = (custos_s - custo_prev_loop) / custo_prev_loop * 100.0
            if dv > 1.0 and dc <= dv + 1.5:
                diagnostico_rel = "🟢 Vendas ↑ e custos controlados / proporcionais"
                classe_rel = "success"
            elif dv > 0 and dc > dv + 1.5:
                diagnostico_rel = "🟠 Vendas ↑ e custos ↑ mais rapidamente"
                classe_rel = "warning"
            elif dv <= 0 and dc >= -1.0:
                diagnostico_rel = "🔴 Vendas ↓ e custos permaneceram altos"
                classe_rel = "danger"
            else:
                diagnostico_rel = "🔵 Vendas ↓ com ajuste proporcional de custos"
                classe_rel = "info"

        evolucao_mensal.append(
            {
                "ano": y_s,
                "mes": m_s,
                "mes_nome": f"{MESES_NOMES_PT[m_s]}/{y_s}",
                "mes_curto": f"{MESES_CURTOS_PT[m_s]}/{str(y_s)[2:]}",
                "vendas_raw": vendas_s,
                "vendas": fmt_brl(vendas_s),
                "custos_raw": custos_s,
                "custos": fmt_brl(custos_s),
                "lucro_raw": lucro_s,
                "lucro": fmt_brl(lucro_s),
                "margem_raw": margem_s,
                "margem": fmt_pct(margem_s),
                "diagnostico_relacao": diagnostico_rel,
                "classe_relacao": classe_rel,
            }
        )
        fat_prev_loop = vendas_s
        custo_prev_loop = custos_s

    grafico_composicao_faturamento_html = ""
    grafico_evolucao_lucro_html = ""

    if gerar_graficos:
        # Seção 14: Gráfico de Pizza "Para onde está indo cada R$ 1,00 vendido?"
        fat_ref_pizza = max(fat_atual, ind_custo_total["atual_raw"], 1.0)
        fatias = [
            {"Item": "Custo de Mercadorias (CMV)", "Valor": max(ind_cmv["atual_raw"], 0.0), "Cor": "#dc2626"},
            {"Item": "Custos Fixos", "Valor": max(ind_fixo["atual_raw"], 0.0), "Cor": "#f59e0b"},
            {"Item": "Custos Variáveis", "Valor": max(ind_variavel["atual_raw"], 0.0), "Cor": "#0284c7"},
            {"Item": "Despesas Financeiras", "Valor": max(ind_financeiro["atual_raw"], 0.0), "Cor": "#7c3aed"},
        ]
        if lucro_atual > 0:
            fatias.append({"Item": "Lucro Líquido", "Valor": lucro_atual, "Cor": "#16a34a"})

        fatias_positivas = [f for f in fatias if f["Valor"] > 0]
        if fatias_positivas:
            df_p = pd.DataFrame(fatias_positivas)
            fig_comp = px.pie(
                df_p,
                names="Item",
                values="Valor",
                color="Item",
                color_discrete_map={f["Item"]: f["Cor"] for f in fatias_positivas},
                hole=0.42,
                title="🍕 Para onde está indo cada R$ 1,00 vendido?",
            )
            fig_comp.update_traces(
                textposition="inside",
                textinfo="percent+label",
                hovertemplate="<b>%{label}</b><br>Valor: R$ %{value:,.2f}<br>Fat. (%): %{percent}<extra></extra>",
            )
            fig_comp.update_layout(
                height=360,
                margin=dict(t=45, b=20, l=15, r=15),
                legend=dict(orientation="h", yanchor="top", y=-0.05, xanchor="center", x=0.5),
            )
            grafico_composicao_faturamento_html = fig_to_html(fig_comp)

        # Seção 15: Gráfico de Linha "EVOLUÇÃO DO LUCRO LÍQUIDO"
        if evolucao_mensal:
            df_ev = pd.DataFrame(evolucao_mensal)
            fig_ev = go.Figure()
            fig_ev.add_trace(
                go.Bar(
                    x=df_ev["mes_curto"],
                    y=df_ev["vendas_raw"],
                    name="Vendas / Faturamento (R$)",
                    marker_color="#cbd5e1",
                )
            )
            fig_ev.add_trace(
                go.Bar(
                    x=df_ev["mes_curto"],
                    y=df_ev["custos_raw"],
                    name="Custos Totais (R$)",
                    marker_color="#f87171",
                )
            )
            fig_ev.add_trace(
                go.Scatter(
                    x=df_ev["mes_curto"],
                    y=df_ev["lucro_raw"],
                    name="Lucro Líquido (R$)",
                    mode="lines+markers+text",
                    text=[f"{m:.1f}%" for m in df_ev["margem_raw"]],
                    textposition="top center",
                    line=dict(color="#15803d", width=3),
                    marker=dict(size=8),
                )
            )
            fig_ev.update_layout(
                barmode="group",
                title="📈 Evolução Mensal: Vendas, Custos, Lucro Líquido e Margem (%)",
                xaxis_title="Mês",
                yaxis_title="Valor (R$)",
                height=360,
                margin=dict(t=45, b=30, l=25, r=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            grafico_evolucao_lucro_html = fig_to_html(fig_ev)

    # -------------------------------------------------------------------------
    # 6. RESUMO EXECUTIVO AUTOMÁTICO ("O QUE ESTÁ ACONTECENDO COM NOSSOS CUSTOS?" - SEÇÃO 23)
    # -------------------------------------------------------------------------
    resumo_executivo = {
        "mes_atual_nome": f"{MESES_NOMES_PT[mes_selecionado]}/{ano_selecionado}",
        "mes_anterior_nome": f"{MESES_NOMES_PT[ym_ant[1]]}/{ym_ant[0]}",
        "faturamento_atual": fmt_brl(fat_atual),
        "faturamento_anterior": fmt_brl(fat_ant),
        "faturamento_var_pct": fmt_pct(fat_var_pct, com_sinal=True),
        "faturamento_subiu": fat_var_pct >= 0,
        "custos_var_pct": ind_custo_total["var_pct"],
        "custos_var_rs": ind_custo_total["var_rs"],
        "custos_subiram": ind_custo_total["var_rs_raw"] > 0,
        "principais_responsaveis": aumentaram[:3] if aumentaram else diminuiram[:3],
        "margem_anterior": fmt_pct(margem_liq_ant),
        "margem_atual": fmt_pct(margem_liq_atual),
    }

    categorias_sugeridas = sorted(
        {
            "Mercadorias / CMV",
            "Pessoal / Folha",
            "Pró-Labore / Sócios",
            "Aluguel / Estrutura",
            "Sistemas / TI",
            "Serviços / Contabilidade",
            "Administrativo",
            "Veículos",
            "Comissões",
            "Bonificações",
            "Fretes / Transportadoras",
            "Impostos",
            "Marketing",
            "Comercial / Vendas",
            "Despesas Financeiras",
            "Despesas Extraordinárias",
        }
        | {v["categoria"] for v in mapa_classificacao.values()}
    )

    return {
        "resumo_executivo": resumo_executivo,
        "faturamento_atual": fmt_brl(fat_atual),
        "faturamento_atual_raw": fat_atual,
        "faturamento_anterior": fmt_brl(fat_ant),
        "faturamento_var_pct": fmt_pct(fat_var_pct, com_sinal=True),
        "ind_cmv": ind_cmv,
        "ind_fixo": ind_fixo,
        "ind_variavel": ind_variavel,
        "ind_financeiro": ind_financeiro,
        "ind_extraordinario": ind_extraordinario,
        "ind_custo_total": ind_custo_total,
        "ind_comissoes_bonif": ind_comissoes_bonif,
        "ind_transporte": ind_transporte,
        "ind_lucro_liquido": ind_lucro_liquido,
        "top_10_custos": top_10_custos,
        "ranking_categorias": ranking_categorias,
        "aumentaram": aumentaram,
        "diminuiram": diminuiram,
        "alertas_custos": alertas_custos,
        "desvios_historico": desvios_historico,
        "alerta_custo_fixo_3m": alerta_custo_fixo_3m,
        "custo_fixo_por_categoria": custo_fixo_por_categoria,
        "custos_fixos_recorrentes": custos_fixos_recorrentes,
        "evolucao_mensal": evolucao_mensal,
        "grafico_composicao_faturamento": grafico_composicao_faturamento_html,
        "grafico_evolucao_lucro": grafico_evolucao_lucro_html,
        "contas_classificadas": sorted(mapa_classificacao.values(), key=lambda x: (x["tipo_custo"], x["categoria"], x["conta_chave"])),
        "categorias_sugeridas": categorias_sugeridas,
        "tipos_custo_choices": ClassificacaoCusto.TIPO_CUSTO_CHOICES,
        "parametros_alerta": {
            "pct": f"{limite_alerta_pct:.1f}",
            "valor": f"{limite_alerta_val:.2f}",
        },
    }
