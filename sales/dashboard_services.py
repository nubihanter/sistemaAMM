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
from django.db.models import Count, Max, Q, Sum
from .models import ContaPagar, ContaReceber, ItemOrcamento, MetaVendedor, NotaFiscal, Orcamento, Vendedor
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

    docs = df["cliente_documento"].fillna("").astype(str).str.replace(r"\D", "", regex=True)
    docs = docs.where(docs.str.len() >= 8, "")
    mask_doc = docs != ""
    if mask_doc.any():
        com_doc = df.loc[mask_doc, ["cliente_nome"]].copy()
        com_doc["_doc_limpo"] = docs[mask_doc]
        if "data_emissao" in df.columns:
            com_doc["data_emissao"] = df.loc[mask_doc, "data_emissao"]
            com_doc = com_doc.sort_values("data_emissao", kind="mergesort")
        mapa_doc_nome = (
            com_doc.drop_duplicates(subset=["_doc_limpo"], keep="last")
            .set_index("_doc_limpo")["cliente_nome"]
        )
        df.loc[mask_doc, "cliente_nome"] = docs[mask_doc].map(mapa_doc_nome)

    return df


def _construir_resolvedor_meta_mes(mes, ano):
    """
    Pré-carrega vínculos Vendedor e MetaVendedor do mês em 2 queries únicas,
    evitando N+1 consultas no loop de ranking.
    """
    vinculos = {
        str(v["nome_hardness"]).strip().upper(): normalizar_nome(v["nome_piperun"] or v["nome_hardness"])
        for v in Vendedor.objects.values("nome_hardness", "nome_piperun")
    }
    metas_por_nome_norm = {}
    for m in MetaVendedor.objects.filter(mes=mes, ano=ano).values("vendedor_nome", "valor"):
        chave = normalizar_nome(m["vendedor_nome"])
        if chave and chave not in metas_por_nome_norm:
            metas_por_nome_norm[chave] = float(m["valor"])

    def resolver(vendedora_selecionada):
        if vendedora_selecionada == "EMPRESA":
            alvo = normalizar_nome(USUARIO_PIPERUN_META_EMPRESA)
        else:
            chave_hard = str(vendedora_selecionada).strip().upper()
            alvo = vinculos.get(chave_hard) or normalizar_nome(chave_hard)
        return metas_por_nome_norm.get(alvo, 0.0)

    return resolver


def obter_meta_vendedor(vendedora_selecionada, mes, ano):
    """
    Busca a meta mensal cruzando nome_hardness -> nome_piperun -> MetaVendedor.
    """
    return _construir_resolvedor_meta_mes(mes, ano)(vendedora_selecionada)


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


def _extrair_prazo_dias_texto(texto_prazo):
    """
    Converte a string de condição/prazo do Hardness ERP (ex: '28', '28/42', '28/35/42',
    '30/60/90', 'A VISTA') no prazo médio em dias da nota fiscal.
    Retorna tuple (prazo_medio_float | None, label_formatado: str).
    """
    if texto_prazo is None:
        return None, "-"
    if isinstance(texto_prazo, (int, float)) and not pd.isna(texto_prazo):
        val = max(0.0, float(texto_prazo))
        return val, ("À Vista (0d)" if val == 0 else f"{val:.0f}d")

    s = str(texto_prazo).strip().upper()
    if not s or s in ("-", "NONE", "NULL", "NAN"):
        return None, "-"
    if "VISTA" in s or "ANTECIP" in s or s == "0":
        return 0.0, "À Vista (0d)"

    nums = [max(0.0, float(x)) for x in re.findall(r"-?\d+", s)]
    if nums:
        media = sum(nums) / len(nums)
        if len(nums) == 1:
            return media, f"{int(round(media))} dias"
        return media, f"{s} ({media:.0f}d)"
    return None, s


def _enriquecer_prazos_notas_mes(df_mes: pd.DataFrame, ano_selecionado: int, mes_selecionado: int) -> pd.DataFrame:
    """
    Enriquece o DataFrame de notas do mês com `prazo_dias` (float/NaN) e `prazo_label` (str),
    extraindo de `dados_brutos['T007_Prazos(T007_Id)']` (ou consultando NotaFiscal / ContaReceber).
    """
    if df_mes.empty:
        df_vazio = df_mes.copy()
        df_vazio["prazo_dias"] = pd.Series(dtype="float64")
        df_vazio["prazo_label"] = pd.Series(dtype="object")
        return df_vazio

    df_out = df_mes.copy()
    mapa_prazo_bruto = {}

    if "dados_brutos" in df_out.columns:
        for num_nf, db in zip(df_out["numero_nota"], df_out["dados_brutos"]):
            if isinstance(db, dict):
                raw_p = db.get("T007_Prazos(T007_Id)") or db.get("prazo") or db.get("prazo_dias")
                if raw_p is not None and str(raw_p).strip():
                    mapa_prazo_bruto[str(num_nf)] = raw_p

    notas_sem_prazo = [
        str(n) for n in df_out["numero_nota"].dropna().unique() if str(n) not in mapa_prazo_bruto
    ]
    if notas_sem_prazo:
        for nf_row in (
            NotaFiscal.objects.exclude(status="CANCELADA")
            .filter(
                data_emissao__year=ano_selecionado,
                data_emissao__month=mes_selecionado,
                numero_nota__in=notas_sem_prazo,
            )
            .values("numero_nota", "dados_brutos")
        ):
            db = nf_row.get("dados_brutos")
            if isinstance(db, dict):
                raw_p = db.get("T007_Prazos(T007_Id)") or db.get("prazo") or db.get("prazo_dias")
                if raw_p is not None and str(raw_p).strip():
                    mapa_prazo_bruto[str(nf_row["numero_nota"])] = raw_p

    # Fallback secundário via ContaReceber (caso alguma nota não tenha T007_Prazos em dados_brutos)
    faltantes_cr = [n for n in notas_sem_prazo if n not in mapa_prazo_bruto]
    if faltantes_cr:
        cr_rows = (
            ContaReceber.objects.filter(
                cancelada=False,
                numero_documento__in=faltantes_cr,
            )
            .values("numero_documento", "prazo_dias", "data_emissao", "data_vencimento")
        )
        prazos_por_doc = {}
        for cr in cr_rows:
            doc = str(cr.get("numero_documento") or "").strip()
            if not doc:
                continue
            p_dias = cr.get("prazo_dias")
            if p_dias is None and cr.get("data_vencimento") and cr.get("data_emissao"):
                p_dias = (cr["data_vencimento"] - cr["data_emissao"]).days
            if p_dias is not None:
                prazos_por_doc.setdefault(doc, []).append(max(0.0, float(p_dias)))
        for doc, lista_p in prazos_por_doc.items():
            if lista_p:
                mapa_prazo_bruto[doc] = "/".join(str(int(round(x))) for x in sorted(lista_p))

    prazos_num = []
    prazos_lbl = []
    for num_nf in df_out["numero_nota"].astype(str):
        p_val, p_lbl = _extrair_prazo_dias_texto(mapa_prazo_bruto.get(num_nf))
        prazos_num.append(np.nan if p_val is None else float(p_val))
        prazos_lbl.append(p_lbl)

    df_out["prazo_dias"] = pd.Series(prazos_num, index=df_out.index, dtype="float64")
    df_out["prazo_label"] = pd.Series(prazos_lbl, index=df_out.index, dtype="object")
    return df_out


def _calcular_resumo_prazo_df(df_sub: pd.DataFrame) -> dict:
    """
    Calcula o prazo médio ponderado pelo valor da NF e o prazo médio simples de um subconjunto de notas.
    """
    if df_sub.empty or "prazo_dias" not in df_sub.columns:
        return {
            "tem_prazo": False,
            "prazo_medio_valor": 0.0,
            "prazo_medio_simples": 0.0,
            "prazo_medio_dias": "Sem dados",
            "prazo_medio_simples_dias": "Sem dados",
            "qtd_notas_com_prazo": 0,
        }

    df_val = df_sub.dropna(subset=["prazo_dias"]).copy()
    if df_val.empty:
        return {
            "tem_prazo": False,
            "prazo_medio_valor": 0.0,
            "prazo_medio_simples": 0.0,
            "prazo_medio_dias": "Sem dados",
            "prazo_medio_simples_dias": "Sem dados",
            "qtd_notas_com_prazo": 0,
        }

    pesos = pd.to_numeric(df_val["valor_total"], errors="coerce").fillna(0.0).clip(lower=0.0)
    prazos = df_val["prazo_dias"].astype(float)
    soma_pesos = float(pesos.sum())
    media_simples = float(prazos.mean())
    if soma_pesos > 0:
        media_pond = float((prazos * pesos).sum() / soma_pesos)
    else:
        media_pond = media_simples

    return {
        "tem_prazo": True,
        "prazo_medio_valor": round(media_pond, 1),
        "prazo_medio_simples": round(media_simples, 1),
        "prazo_medio_dias": f"{media_pond:.1f} dias",
        "prazo_medio_simples_dias": f"{media_simples:.1f} dias",
        "qtd_notas_com_prazo": int(len(df_val)),
    }


def gerar_metricas_e_graficos(
    df,
    vendedora_selecionada,
    mes_selecionado,
    ano_selecionado,
    incluir_margem_admin=False,
    gerar_graficos=True,
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
        "custo_total_mes": "R$ 0,00",
        "tem_margem": False,
        "tem_prazo": False,
        "prazo_medio_dias": "0.0 dias",
        "prazo_medio_valor": 0.0,
        "prazo_medio_simples_dias": "0.0 dias",
        "qtd_notas_com_prazo": 0,
        "grafico_prazo_vendedores": "",
        "comparativo_prazo_vendedores": [],
    }

    if df.empty:
        return metricas_vazias, "", "", "", "", "", []

    df = df.copy()
    df["data_emissao"] = pd.to_datetime(df["data_emissao"])
    df["valor_total"] = pd.to_numeric(df["valor_total"], errors="coerce").fillna(0.0)
    if "vendedor_nome" in df.columns:
        df["vendedor_nome"] = df["vendedor_nome"].fillna("").astype(str).str.strip().str.upper()
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
    dt_ini_mom = pd.Timestamp(year=ano_ant, month=mes_ant, day=1)
    dt_fim_mom = data_inicio - timedelta(days=1)

    # Mesmo mês do ano anterior (para YoY)
    dt_ini_yoy = pd.Timestamp(year=ano_selecionado - 1, month=mes_selecionado, day=1)
    if mes_selecionado == 12:
        dt_fim_yoy = pd.Timestamp(year=ano_selecionado, month=1, day=1) - timedelta(days=1)
    else:
        dt_fim_yoy = pd.Timestamp(year=ano_selecionado - 1, month=mes_selecionado + 1, day=1) - timedelta(days=1)

    # Enriquecimento de prazos médios para todas as notas do mês selecionado
    df_periodo_geral = df[
        (df["data_emissao"] >= data_inicio)
        & (df["data_emissao"] <= data_fim)
    ].copy()
    df_periodo_geral = _enriquecer_prazos_notas_mes(df_periodo_geral, ano_selecionado, mes_selecionado)

    # Filtro de dados da visão atual
    if vendedora_selecionada == "EMPRESA":
        df_vendedor = df
        df_filtered = df_periodo_geral.copy()
    else:
        df_vendedor = df[df["vendedor_nome"] == vendedora_selecionada]
        df_filtered = df_periodo_geral[df_periodo_geral["vendedor_nome"] == vendedora_selecionada].copy()

    total_vendas = float(df_filtered["valor_total"].sum())
    num_vendas = len(df_filtered)
    num_clientes = int(df_filtered["cliente_nome"].nunique()) if not df_filtered.empty else 0
    ticket_medio = total_vendas / num_vendas if num_vendas > 0 else 0.0

    # Prazo médio praticado na visão selecionada (Vendedor individual ou Empresa)
    resumo_prazo_visao = _calcular_resumo_prazo_df(df_filtered)

    # Comparativo de Prazo Médio entre todos os Vendedores Ativos no mês
    vendedores_ativos_set = {
        str(v).strip().upper()
        for v in Vendedor.objects.filter(ativo=True).values_list("nome_hardness", flat=True)
        if v and str(v).strip().upper() not in ("", "NAN", "NONE", "DESCONHECIDO")
    }
    if not vendedores_ativos_set and "vendedor_nome" in df_periodo_geral.columns:
        vendedores_ativos_set = {
            str(v).strip().upper()
            for v in df_periodo_geral["vendedor_nome"].dropna().unique()
            if str(v).strip().upper() not in ("", "NAN", "NONE", "DESCONHECIDO")
        }

    comparativo_prazo_vendedores = []
    if not df_periodo_geral.empty and "vendedor_nome" in df_periodo_geral.columns:
        for vend_nome, grp_v in df_periodo_geral.groupby("vendedor_nome"):
            if not vend_nome or vend_nome in ("", "NAN", "NONE", "DESCONHECIDO"):
                continue
            if vendedores_ativos_set and vend_nome not in vendedores_ativos_set:
                continue
            rp_vend = _calcular_resumo_prazo_df(grp_v)
            if not rp_vend["tem_prazo"]:
                continue
            comparativo_prazo_vendedores.append(
                {
                    "Vendedor": vend_nome,
                    "Prazo_Medio_Dias": rp_vend["prazo_medio_valor"],
                    "Prazo_Simples_Dias": rp_vend["prazo_medio_simples"],
                    "Qtd_NFs": rp_vend["qtd_notas_com_prazo"],
                    "Total_Vendas": float(grp_v["valor_total"].sum()),
                }
            )
    comparativo_prazo_vendedores.sort(key=lambda x: x["Prazo_Medio_Dias"], reverse=True)

    # Comparativo MoM e YoY
    vendas_mom = float(
        df_vendedor[
            (df_vendedor["data_emissao"] >= dt_ini_mom)
            & (df_vendedor["data_emissao"] <= dt_fim_mom)
        ]["valor_total"].sum()
    )
    vendas_yoy = float(
        df_vendedor[
            (df_vendedor["data_emissao"] >= dt_ini_yoy)
            & (df_vendedor["data_emissao"] <= dt_fim_yoy)
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

    # Meta do Vendedor / Empresa (com resolvedor em lote para evitar N+1 no ranking)
    resolver_meta_mes = _construir_resolvedor_meta_mes(mes_selecionado, ano_selecionado)
    meta_periodo = resolver_meta_mes(vendedora_selecionada)
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

    dados_margem = {"tem_dados": False, "lucro_bruto": "R$ 0,00", "margem_pct": "0.0%", "custo_total": "R$ 0,00"}
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
        "custo_total_mes": dados_margem.get("custo_total", "R$ 0,00"),
        "tem_margem": dados_margem["tem_dados"],
        "tem_prazo": resumo_prazo_visao["tem_prazo"],
        "prazo_medio_dias": resumo_prazo_visao["prazo_medio_dias"],
        "prazo_medio_valor": resumo_prazo_visao["prazo_medio_valor"],
        "prazo_medio_simples_dias": resumo_prazo_visao["prazo_medio_simples_dias"],
        "qtd_notas_com_prazo": resumo_prazo_visao["qtd_notas_com_prazo"],
        "grafico_prazo_vendedores": "",
        "comparativo_prazo_vendedores": comparativo_prazo_vendedores,
    }

    # Tabela de Notas Fiscais do Período
    tabela_notas = []
    if not df_filtered.empty:
        df_nf_tab = df_filtered.sort_values(["data_emissao", "numero_nota"], ascending=[False, False]).copy()
        df_nf_tab["data_str"] = df_nf_tab["data_emissao"].dt.strftime("%d/%m/%Y")
        df_nf_tab["data_iso"] = df_nf_tab["data_emissao"].dt.strftime("%Y-%m-%d")
        df_nf_tab["prazo_dias_order"] = df_nf_tab["prazo_dias"].fillna(-1.0)
        tabela_notas = df_nf_tab[
            [
                "numero_nota",
                "cliente_nome",
                "vendedor_nome",
                "data_str",
                "data_iso",
                "valor_total",
                "prazo_label",
                "prazo_dias_order",
            ]
        ].to_dict(orient="records")

    if not gerar_graficos:
        return metricas, "", "", "", "", "", tabela_notas

    # Gráfico Comparativo de Prazo Médio entre Vendedores Ativos (para Admin / Supervisor)
    if comparativo_prazo_vendedores:
        df_prazo_vend = pd.DataFrame(comparativo_prazo_vendedores)
        resumo_empresa_prazo = _calcular_resumo_prazo_df(df_periodo_geral)
        media_emp_dias = resumo_empresa_prazo["prazo_medio_valor"]

        fig_prazo = px.bar(
            df_prazo_vend,
            x="Vendedor",
            y="Prazo_Medio_Dias",
            text="Prazo_Medio_Dias",
            color="Prazo_Medio_Dias",
            color_continuous_scale="Tealgrn",
            title=f"⏱️ Comparativo de Prazo Médio Praticado por Vendedor Ativo ({mes_selecionado:02d}/{ano_selecionado})",
            labels={
                "Prazo_Medio_Dias": "Prazo Médio Ponderado (dias)",
                "Vendedor": "Vendedor Ativo",
                "Qtd_NFs": "Qtd NFs",
            },
            hover_data={
                "Prazo_Medio_Dias": ":.1f",
                "Prazo_Simples_Dias": ":.1f",
                "Qtd_NFs": True,
            },
        )
        fig_prazo.update_traces(texttemplate="%{text:.1f}d", textposition="outside")
        if media_emp_dias > 0:
            fig_prazo.add_hline(
                y=media_emp_dias,
                line_dash="dash",
                line_color="#dc2626",
                annotation_text=f"Média Empresa ({media_emp_dias:.1f} dias)",
                annotation_position="top right",
            )
        max_y_prazo = float(df_prazo_vend["Prazo_Medio_Dias"].max()) if not df_prazo_vend.empty else 30.0
        fig_prazo.update_layout(
            height=360,
            margin=dict(t=50, b=60, l=25, r=20),
            coloraxis_showscale=False,
            yaxis=dict(
                title="Prazo Médio (dias)",
                ticksuffix=" d",
                range=[0, max(max_y_prazo * 1.22, 35.0)],
            ),
            xaxis_title="Vendedor Ativo",
        )
        metricas["grafico_prazo_vendedores"] = fig_to_html(fig_prazo)

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
        (df["data_emissao"] >= data_inicio)
        & (df["data_emissao"] <= data_fim)
    ]

    ranking_data = []
    if "vendedor_nome" in df.columns:
        vendedores_permitidos_ranking = set(
            Vendedor.objects.filter(ativo=True, ativo_ranking=True).values_list("nome_hardness", flat=True)
        )
        vendedores_ranking = [
            v
            for v in df["vendedor_nome"].unique()
            if v not in ["", "NAN", "NONE"] and v in vendedores_permitidos_ranking
        ]
        vendas_por_vendedor_mes = (
            df_periodo_geral.groupby("vendedor_nome")["valor_total"].sum().to_dict()
            if not df_periodo_geral.empty
            else {}
        )

        for vend in vendedores_ranking:
            total_vend = float(vendas_por_vendedor_mes.get(vend, 0.0))
            meta_vend = resolver_meta_mes(vend)
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
            (df_vendedor["data_emissao"] >= dt_m_ini)
            & (df_vendedor["data_emissao"] <= dt_m_fim)
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
    Calcula de forma vetorizada o faturamento médio mensal retroativo à ÚLTIMA VENDA de cada cliente.
    Se o cliente tiver histórico menor que 6 meses, divide apenas pelos meses decorridos.
    """
    if df.empty:
        return {}

    extremos = df.groupby("cliente_nome")["data_emissao"].agg(prim_data="min", ult_data="max")
    extremos["janela_inicio"] = extremos["ult_data"] - pd.Timedelta(days=180)

    janela_por_linha = df["cliente_nome"].map(extremos["janela_inicio"])
    vendas_janela = (
        df.loc[df["data_emissao"] >= janela_por_linha]
        .groupby("cliente_nome")["valor_total"]
        .sum()
    )
    extremos["vendas_janela"] = vendas_janela.reindex(extremos.index, fill_value=0.0)

    meses_decorridos = (
        (extremos["ult_data"].dt.year - extremos["prim_data"].dt.year) * 12
        + (extremos["ult_data"].dt.month - extremos["prim_data"].dt.month)
        + 1
    ).clip(lower=1.0, upper=6.0)

    divisor = np.where(extremos["prim_data"] <= extremos["janela_inicio"], 6.0, meses_decorridos)
    ticket_series = extremos["vendas_janela"] / divisor
    return ticket_series.to_dict()


def gerar_analise_clientes(
    df,
    vendedora_selecionada="EMPRESA",
    filtros_curva=None,
    filtros_status=None,
    gerar_graficos=True,
):
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

    # 1. Histórico GLOBAL de cada cliente ordenado por data crescente (sort estável)
    df_ordenado_global = df.sort_values(["data_emissao", "valor_total"], kind="mergesort")

    vend_limpo = df_ordenado_global["vendedor_nome"].fillna("").astype(str).str.strip().str.upper()
    mask_vend_valido = ~vend_limpo.isin(["", "NAN", "NONE", "DESCONHECIDO"])
    ultimo_vendedor_map = (
        df_ordenado_global.loc[mask_vend_valido, ["cliente_nome"]]
        .assign(_vend=vend_limpo[mask_vend_valido])
        .drop_duplicates(subset=["cliente_nome"], keep="last")
        .set_index("cliente_nome")["_vend"]
    )

    df_clientes = (
        df_ordenado_global.groupby("cliente_nome", as_index=False)
        .agg(
            Faturamento_Total=("valor_total", "sum"),
            Num_Vendas=("valor_total", "count"),
            Ultima_Venda=("data_emissao", "max"),
            Primeira_Venda=("data_emissao", "min"),
        )
    )
    df_clientes["Vendedora"] = df_clientes["cliente_nome"].map(ultimo_vendedor_map).fillna("-")

    # 2. Cálculo Vetorizado do Ticket Médio 6m Individual por Cliente
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

    tm = df_clientes["Ticket_Medio_6m"]
    df_clientes["Curva"] = np.select(
        [tm >= 5000, tm >= 3000, tm >= 1500, tm >= 700],
        ["AA", "A", "B", "C"],
        default="D",
    )

    dias_inat = df_clientes["Dias_Inatividade"]
    df_clientes["Status"] = np.select(
        [
            df_clientes["Primeira_Venda"].dt.year == ano_vigente,
            dias_inat <= 30,
            dias_inat <= 90,
        ],
        ["Novo", "Ativo", "Em Risco"],
        default="Inativo",
    )

    curvas_aplicadas = filtros_curva or ["AA", "A", "B"]
    status_aplicados = filtros_status or ["Novo", "Ativo", "Em Risco", "Inativo"]
    df_filtrado = df_clientes[
        df_clientes["Curva"].isin(curvas_aplicadas) & df_clientes["Status"].isin(status_aplicados)
    ].copy()

    # 5. KPIs da Carteira
    kpis_carteira = {
        "total_clientes": len(df_clientes),
        "clientes_ativos": int((df_clientes["Status"] == "Ativo").sum()),
        "clientes_risco": int((df_clientes["Status"] == "Em Risco").sum()),
        "clientes_inativos": int((df_clientes["Status"] == "Inativo").sum()),
        "clientes_novos": int((df_clientes["Status"] == "Novo").sum()),
        "prioritarios": int(df_clientes["Curva"].isin(["AA", "A", "B"]).sum()),
        "faturamento_carteira": f"R$ {df_clientes['Faturamento_Total'].sum():,.2f}",
        "potencial_risco_mensal": f"R$ {df_clientes.loc[df_clientes['Status'] == 'Em Risco', 'Ticket_Medio_6m'].sum():,.2f}",
    }

    # 6. Gráficos de Prioridade
    grafico_status_html = ""
    grafico_matriz_html = ""
    df_prioridade = df_filtrado

    if gerar_graficos and not df_prioridade.empty:
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


MOTIVOS_PERDA_PADRAO_D047 = {
    "39": "DESACORDO COMERCIAL",
    "40": "ERRO DE PREENCHIMENTO",
    "41": "PREÇO DE CONCORRENTE",
    "42": "ORÇAMENTO DUPLICADO",
    "43": "DESISTÊNCIA DO CLIENTE",
    "44": "TESTE DO SISTEMA",
    "45": "PREÇO",
    "46": "PRAZO DE ENTREGA",
    "47": "ESTOQUE",
    "48": "PRODUTO C/A",
    "49": "DUPLICADO",
}

D047_MOTIVOS_DESCONSIDERADOS = {
    "40",  # ERRO DE PREENCHIMENTO
    "42",  # ORÇAMENTO DUPLICADO
    "44",  # TESTE DO SISTEMA
    "49",  # DUPLICADO
}

PADROES_MOTIVOS_DESCONSIDERADOS = [
    r"\bDUPLICAD",
    r"\bTESTE",
    r"ERRO DE PREENCHIMENTO",
    r"ORCAMENTO DUPLICADO",
]
RE_MOTIVOS_DESCONSIDERADOS = re.compile("|".join(PADROES_MOTIVOS_DESCONSIDERADOS), re.IGNORECASE)


def eh_motivo_desconsiderado(dados_brutos, motivo_perda="", observacao="", status_consolidado="") -> bool:
    """
    Retorna True se o orçamento tiver motivo que não deve ser computado na tela de orçamentos:
    duplicado, teste, erro de preenchimento, orçamento duplicado.
    """
    if str(status_consolidado).strip().upper() == "FINALIZADO":
        return False

    cod_d047 = ""
    if isinstance(dados_brutos, dict):
        cod_d047 = str(dados_brutos.get("T003_D047_Id") or "").strip()

    if cod_d047 in D047_MOTIVOS_DESCONSIDERADOS:
        return True

    textos = f"{motivo_perda or ''} {observacao or ''}".strip()
    if not textos:
        return False

    nfkd = unicodedata.normalize("NFKD", textos)
    texto_norm = "".join(c for c in nfkd if not unicodedata.combining(c)).upper()

    return bool(RE_MOTIVOS_DESCONSIDERADOS.search(texto_norm))


def resolver_motivo_perda_padrao(dados_brutos, status_consolidado: str = "") -> str:
    """
    Extrai a descrição padronizada do Motivo de Perda (tabela D047 / campo T003_D047_Id do Hardness ERP).
    """
    if isinstance(dados_brutos, dict):
        ja_resolvido = str(dados_brutos.get("motivo_perda_padrao") or "").strip()
        if ja_resolvido:
            return ja_resolvido
        cod = str(dados_brutos.get("T003_D047_Id") or "").strip()
        if cod and cod in MOTIVOS_PERDA_PADRAO_D047:
            return MOTIVOS_PERDA_PADRAO_D047[cod]
        if cod and cod not in ("0", "NONE", "NULL", "NAN"):
            return f"CÓD. {cod}"
    if status_consolidado == "PERDIDO":
        return "NÃO CLASSIFICADO"
    return "-"


def gerar_dashboard_orcamentos(
    mes_selecionado: int,
    ano_selecionado: int,
    vendedora_selecionada: str = "EMPRESA",
    empresa_filtro: str = "TODAS",
    status_filtro: str = "TODOS",
    gerar_graficos: bool = True,
):
    """
    Calcula os indicadores principais de Orçamentos do mês:
    - Total de orçamentos no mês (R$ e quantidade)
    - % de orçamentos realizados / ganhos (em R$ e em quantidade)
    - Orçamentos em aberto (pendentes) e perdidos/cancelados (R$, % e quantidade)
    - Ranking / conversão por vendedor (apenas vendedores ativos) e tabela de orçamentos do período
      com o campo padronizado de Motivo de Perda (T003_D047_Id) além da Observação.
    Desconsidera dos cálculos e tabelas os motivos: duplicado, teste, erro de preenchimento, orçamento duplicado.
    """
    # Exclui na raiz do banco qualquer orçamento cancelado ou com motivos desconsiderados (duplicado, teste, erro de preenchimento)
    qs = Orcamento.objects.filter(
        data_emissao__year=ano_selecionado,
        data_emissao__month=mes_selecionado,
    ).exclude(status="CANCELADO").exclude(
        Q(dados_brutos__T003_D047_Id__in=["40", "42", "44", "49", 40, 42, 44, 49])
        | Q(motivo_perda__icontains="duplicad")
        | Q(motivo_perda__icontains="teste")
        | Q(motivo_perda__icontains="erro de preenchimento")
        | Q(motivo_perda__icontains="orçamento duplicado")
        | Q(observacao__icontains="duplicad")
        | Q(observacao__icontains="teste")
        | Q(observacao__icontains="erro de preenchimento")
        | Q(observacao__icontains="orçamento duplicado")
    )
    if empresa_filtro and empresa_filtro != "TODAS":
        qs = qs.filter(empresa=empresa_filtro)

    registros_mes = list(
        qs.values(
            "numero_orcamento",
            "empresa",
            "data_emissao",
            "cliente_nome",
            "cidade",
            "uf",
            "vendedor_nome",
            "valor_total",
            "valor_custo",
            "percentual_margem",
            "ipv",
            "status",
            "flag_status",
            "flag_perdido",
            "motivo_perda",
            "pedido_gerado",
            "numero_nota",
            "observacao",
            "dados_brutos",
        )
    )

    kpis_vazios = {
        "qtd_total": 0,
        "valor_orcado": "R$ 0,00",
        "valor_orcado_raw": 0.0,
        "ticket_medio": "R$ 0,00",
        "qtd_realizados": 0,
        "valor_realizado": "R$ 0,00",
        "valor_realizado_raw": 0.0,
        "pct_realizado_valor": "0.0%",
        "pct_realizado_valor_raw": 0.0,
        "pct_realizado_qtd": "0.0%",
        "qtd_pendentes": 0,
        "valor_pendente": "R$ 0,00",
        "pct_pendente_valor": "0.0%",
        "qtd_perdidos": 0,
        "valor_perdido": "R$ 0,00",
        "valor_perdido_raw": 0.0,
        "pct_perdido_valor": "0.0%",
        "margem_realizados_pct": "0.0%",
        "lucro_bruto_realizados": "R$ 0,00",
        "ranking_preco_total_produtos": 0,
        "ranking_preco_total_qtd": "0",
        "ranking_preco_total_valor": "R$ 0,00",
        "ranking_preco_total_orcs": 0,
    }

    if not registros_mes:
        return kpis_vazios, "", "", "", [], [], []

    df_all = pd.DataFrame(registros_mes)
    df_all["valor_total"] = pd.to_numeric(df_all["valor_total"], errors="coerce").fillna(0.0).astype(float)
    df_all["valor_custo"] = pd.to_numeric(df_all["valor_custo"], errors="coerce").fillna(0.0).astype(float)
    df_all["percentual_margem"] = pd.to_numeric(df_all["percentual_margem"], errors="coerce").fillna(0.0).astype(float)
    df_all["ipv"] = pd.to_numeric(df_all["ipv"], errors="coerce").fillna(0.0).astype(float)
    df_all["vendedor_nome"] = (
        df_all["vendedor_nome"]
        .fillna("DESCONHECIDO")
        .astype(str)
        .str.strip()
        .str.upper()
        .replace({"": "DESCONHECIDO", "NAN": "DESCONHECIDO", "NONE": "DESCONHECIDO"})
    )
    df_all["cliente_nome"] = (
        df_all["cliente_nome"]
        .fillna("CLIENTE NÃO IDENTIFICADO")
        .astype(str)
        .str.strip()
        .replace({"": "CLIENTE NÃO IDENTIFICADO", "NAN": "CLIENTE NÃO IDENTIFICADO"})
    )

    # Normaliza status consolidado: FINALIZADO (Realizado), PENDENTE (Em Aberto), PERDIDO (Perdido/Cancelado)
    flag_p = df_all["flag_perdido"].fillna("").astype(str).str.strip().str.upper()
    st_raw = df_all["status"].fillna("PENDENTE").astype(str).str.strip().str.upper()
    df_all["status_consolidado"] = np.select(
        [
            st_raw == "FINALIZADO",
            (flag_p == "S") | st_raw.isin(["PERDIDO", "CANCELADO"]),
        ],
        ["FINALIZADO", "PERDIDO"],
        default="PENDENTE",
    )

    df_all["motivo_perda_padrao"] = [
        resolver_motivo_perda_padrao(db, st)
        for db, st in zip(df_all["dados_brutos"], df_all["status_consolidado"])
    ]

    # Identifica orçamentos com motivos que não devem ser computados na tela de orçamentos:
    # duplicado, teste, erro de preenchimento, orçamento duplicado, ou com status CANCELADO
    df_all["desconsiderar"] = [
        (st_orig == "CANCELADO") or eh_motivo_desconsiderado(db, mp, obs, sc)
        for db, mp, obs, sc, st_orig in zip(
            df_all["dados_brutos"],
            df_all["motivo_perda"],
            df_all["observacao"],
            df_all["status_consolidado"],
            st_raw,
        )
    ]

    # Filtra pela visão selecionada (EMPRESA ou Vendedor específico)
    if vendedora_selecionada and vendedora_selecionada != "EMPRESA":
        df_visao_raw = df_all[df_all["vendedor_nome"] == vendedora_selecionada.strip().upper()].copy()
    else:
        df_visao_raw = df_all.copy()

    # Não computa orçamentos de teste, duplicados ou com erro de preenchimento
    df_visao = df_visao_raw[~df_visao_raw["desconsiderar"]].copy()

    if df_visao.empty:
        return kpis_vazios, "", "", "", [], [], []

    qtd_total = len(df_visao)
    valor_orcado = float(df_visao["valor_total"].sum())
    ticket_medio = (valor_orcado / qtd_total) if qtd_total > 0 else 0.0

    df_real = df_visao[df_visao["status_consolidado"] == "FINALIZADO"]
    df_pend = df_visao[df_visao["status_consolidado"] == "PENDENTE"]
    df_perd = df_visao[df_visao["status_consolidado"] == "PERDIDO"]

    qtd_realizados = len(df_real)
    valor_realizado = float(df_real["valor_total"].sum())
    pct_realizado_valor = (valor_realizado / valor_orcado * 100.0) if valor_orcado > 0 else 0.0
    pct_realizado_qtd = (qtd_realizados / qtd_total * 100.0) if qtd_total > 0 else 0.0

    qtd_pendentes = len(df_pend)
    valor_pendente = float(df_pend["valor_total"].sum())
    pct_pendente_valor = (valor_pendente / valor_orcado * 100.0) if valor_orcado > 0 else 0.0

    qtd_perdidos = len(df_perd)
    valor_perdido = float(df_perd["valor_total"].sum())
    pct_perdido_valor = (valor_perdido / valor_orcado * 100.0) if valor_orcado > 0 else 0.0

    custo_realizados = float(df_real["valor_custo"].sum())
    lucro_bruto_real = max(valor_realizado - custo_realizados, 0.0) if custo_realizados > 0 else 0.0
    margem_real_pct = (
        ((valor_realizado - custo_realizados) / valor_realizado * 100.0)
        if (valor_realizado > 0 and custo_realizados > 0)
        else 0.0
    )

    kpis = {
        "qtd_total": qtd_total,
        "valor_orcado": f"R$ {valor_orcado:,.2f}",
        "valor_orcado_raw": valor_orcado,
        "ticket_medio": f"R$ {ticket_medio:,.2f}",
        "qtd_realizados": qtd_realizados,
        "valor_realizado": f"R$ {valor_realizado:,.2f}",
        "valor_realizado_raw": valor_realizado,
        "pct_realizado_valor": f"{pct_realizado_valor:.1f}%",
        "pct_realizado_valor_raw": pct_realizado_valor,
        "pct_realizado_qtd": f"{pct_realizado_qtd:.1f}%",
        "qtd_pendentes": qtd_pendentes,
        "valor_pendente": f"R$ {valor_pendente:,.2f}",
        "pct_pendente_valor": f"{pct_pendente_valor:.1f}%",
        "qtd_perdidos": qtd_perdidos,
        "valor_perdido": f"R$ {valor_perdido:,.2f}",
        "valor_perdido_raw": valor_perdido,
        "pct_perdido_valor": f"{pct_perdido_valor:.1f}%",
        "margem_realizados_pct": f"{margem_real_pct:.1f}%",
        "lucro_bruto_realizados": f"R$ {lucro_bruto_real:,.2f}",
    }

    # Filtra vendedores inativos dos gráficos e do ranking de vendedores
    vendedores_ativos_grafico = {
        str(v).strip().upper()
        for v in Vendedor.objects.filter(ativo=True, ativo_ranking=True).values_list("nome_hardness", flat=True)
        if v
    }
    if not vendedores_ativos_grafico:
        vendedores_ativos_grafico = {
            str(v).strip().upper()
            for v in Vendedor.objects.filter(ativo=True).values_list("nome_hardness", flat=True)
            if v
        }

    # Resumo por Vendedor (apenas vendedores ativos quando na visão EMPRESA)
    resumo_vendedores = []
    for vend, grp in df_visao.groupby("vendedor_nome"):
        if (
            vendedora_selecionada == "EMPRESA"
            and vendedores_ativos_grafico
            and vend not in vendedores_ativos_grafico
        ):
            continue

        v_tot = float(grp["valor_total"].sum())
        q_tot = len(grp)
        g_real = grp[grp["status_consolidado"] == "FINALIZADO"]
        g_pend = grp[grp["status_consolidado"] == "PENDENTE"]
        g_perd = grp[grp["status_consolidado"] == "PERDIDO"]

        v_real = float(g_real["valor_total"].sum())
        q_real = len(g_real)
        v_pend = float(g_pend["valor_total"].sum())
        q_pend = len(g_pend)
        v_perd = float(g_perd["valor_total"].sum())
        q_perd = len(g_perd)

        pct_val = (v_real / v_tot * 100.0) if v_tot > 0 else 0.0
        pct_q = (q_real / q_tot * 100.0) if q_tot > 0 else 0.0

        resumo_vendedores.append(
            {
                "vendedor_nome": vend,
                "qtd_total": q_tot,
                "valor_orcado": v_tot,
                "qtd_realizados": q_real,
                "valor_realizado": v_real,
                "qtd_pendentes": q_pend,
                "valor_pendente": v_pend,
                "qtd_perdidos": q_perd,
                "valor_perdido": v_perd,
                "pct_realizado_valor": pct_val,
                "pct_realizado_qtd": pct_q,
            }
        )
    resumo_vendedores.sort(key=lambda r: r["valor_realizado"], reverse=True)

    # Gráficos Plotly
    # Gráficos Plotly
    grafico_status_html = ""
    grafico_vendedores_html = ""
    grafico_causas_perda_html = ""

    if gerar_graficos:
        df_status_chart = pd.DataFrame(
            [
                {"Status": "Realizado (Ganho)", "Valor": valor_realizado, "Qtd": qtd_realizados},
                {"Status": "Em Aberto (Pendente)", "Valor": valor_pendente, "Qtd": qtd_pendentes},
                {"Status": "Perdido (Comercial)", "Valor": valor_perdido, "Qtd": qtd_perdidos},
            ]
        )
        fig_st = px.bar(
            df_status_chart,
            x="Status",
            y="Valor",
            color="Status",
            text_auto=".2s",
            title=f"📊 Orçamentos por Status em R$ ({mes_selecionado:02d}/{ano_selecionado})",
            labels={"Valor": "Valor Total (R$)", "Status": "Situação do Orçamento"},
            color_discrete_map={
                "Realizado (Ganho)": "#15803d",
                "Em Aberto (Pendente)": "#f59e0b",
                "Perdido (Comercial)": "#dc2626",
            },
        )
        fig_st.update_layout(showlegend=False, height=340, margin=dict(t=45, b=20, l=20, r=20))
        grafico_status_html = fig_to_html(fig_st)

        # Gráfico de Pizza: Causas de Perdas Comerciais em R$
        df_perdidos = df_visao[df_visao["status_consolidado"] == "PERDIDO"].copy()
        if not df_perdidos.empty:
            df_causas = (
                df_perdidos.groupby("motivo_perda_padrao", as_index=False)
                .agg(Valor=("valor_total", "sum"), Qtd=("numero_orcamento", "count"))
                .sort_values(by="Valor", ascending=False)
            )
            df_causas["Motivo"] = df_causas["motivo_perda_padrao"].astype(str).str.title()
            fig_causas = px.pie(
                df_causas,
                names="Motivo",
                values="Valor",
                title=f"🎯 Causas de Perdas Comerciais em R$ ({mes_selecionado:02d}/{ano_selecionado})",
                hole=0.45,
                color_discrete_sequence=[
                    "#dc2626",
                    "#f97316",
                    "#f59e0b",
                    "#eab308",
                    "#64748b",
                    "#8b5cf6",
                    "#06b6d4",
                ],
            )
            fig_causas.update_traces(
                textposition="inside",
                textinfo="percent+label",
                hovertemplate="<b>%{label}</b><br>Perda: R$ %{value:,.2f}<br>Participação: %{percent}<extra></extra>",
            )
            fig_causas.update_layout(
                height=340,
                margin=dict(t=45, b=20, l=20, r=20),
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=-0.28, xanchor="center", x=0.5),
            )
            grafico_causas_perda_html = fig_to_html(fig_causas)

        if resumo_vendedores:
            df_vend_chart = pd.DataFrame(resumo_vendedores).head(12)
            fig_vend = go.Figure()
            fig_vend.add_trace(
                go.Bar(
                    x=df_vend_chart["vendedor_nome"],
                    y=df_vend_chart["valor_orcado"],
                    name="Total Orçado (R$)",
                    marker_color="#94a3b8",
                )
            )
            fig_vend.add_trace(
                go.Bar(
                    x=df_vend_chart["vendedor_nome"],
                    y=df_vend_chart["valor_realizado"],
                    name="Realizado / Ganho (R$)",
                    marker_color="#15803d",
                    text=[f"{p:.1f}%" for p in df_vend_chart["pct_realizado_valor"]],
                    textposition="outside",
                )
            )
            fig_vend.update_layout(
                barmode="group",
                title="🏆 Orçado vs Realizado (R$) e % Conversão por Vendedor",
                yaxis_title="Valor (R$)",
                xaxis_title="Vendedor",
                height=340,
                margin=dict(t=45, b=30, l=20, r=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            grafico_vendedores_html = fig_to_html(fig_vend)

    # -------------------------------------------------------------
    # Ranking de Produtos Não Vendidos pelo Motivo Preço
    # -------------------------------------------------------------
    df_perdidos_preco = df_visao[
        (df_visao["status_consolidado"] == "PERDIDO")
        & (
            df_visao["motivo_perda_padrao"].str.upper().str.contains("PREÇO|PRECO", na=False)
            | df_visao["motivo_perda"].str.upper().str.contains("PREÇO|PRECO|CONCORRENTE", na=False)
            | df_visao["observacao"].str.upper().str.contains("PREÇO|PRECO|CONCORRENTE", na=False)
            | df_visao["dados_brutos"].apply(
                lambda d: str(d.get("T003_D047_Id") or "") in ("41", "45") if isinstance(d, dict) else False
            )
        )
    ]
    numeros_orcs_preco = set(df_perdidos_preco["numero_orcamento"].astype(str).unique())

    ranking_produtos_preco = []
    kpi_ranking_total_produtos = 0
    kpi_ranking_total_qtd = 0.0
    kpi_ranking_total_valor = 0.0

    if numeros_orcs_preco:
        qs_itens = ItemOrcamento.objects.filter(
            numero_orcamento__in=numeros_orcs_preco,
            data_emissao__year=ano_selecionado,
            data_emissao__month=mes_selecionado,
        )
        if empresa_filtro and empresa_filtro != "TODAS":
            qs_itens = qs_itens.filter(empresa=empresa_filtro)
        if vendedora_selecionada and vendedora_selecionada != "EMPRESA":
            qs_itens = qs_itens.filter(vendedor_nome=vendedora_selecionada.strip().upper())

        itens_agg = list(
            qs_itens.values("codigo_produto", "descricao_produto")
            .annotate(
                total_qtd=Sum("quantidade"),
                total_valor=Sum("valor_total"),
                total_orcs=Count("numero_orcamento", distinct=True),
                marca_prod=Max("marca"),
            )
            .order_by("-total_valor")
        )

        max_val_top = float(itens_agg[0]["total_valor"] or 1.0) if itens_agg else 1.0

        for idx, item in enumerate(itens_agg, start=1):
            t_qtd = float(item["total_qtd"] or 0.0)
            t_val = float(item["total_valor"] or 0.0)
            p_med = (t_val / t_qtd) if t_qtd > 0 else 0.0
            pct_barra = min(100.0, (t_val / max_val_top * 100.0)) if max_val_top > 0 else 0.0

            kpi_ranking_total_qtd += t_qtd
            kpi_ranking_total_valor += t_val

            ranking_produtos_preco.append(
                {
                    "posicao": idx,
                    "codigo_produto": item["codigo_produto"],
                    "descricao_produto": item["descricao_produto"],
                    "marca": item.get("marca_prod") or "-",
                    "quantidade": t_qtd,
                    "valor_total": t_val,
                    "valor_total_fmt": f"R$ {t_val:,.2f}",
                    "preco_medio": p_med,
                    "preco_medio_fmt": f"R$ {p_med:,.2f}",
                    "total_orcs": item["total_orcs"],
                    "pct_barra": round(pct_barra, 1),
                }
            )

        kpi_ranking_total_produtos = len(ranking_produtos_preco)

    kpis["ranking_preco_total_produtos"] = kpi_ranking_total_produtos
    kpis["ranking_preco_total_qtd"] = f"{kpi_ranking_total_qtd:,.0f}"
    kpis["ranking_preco_total_valor"] = f"R$ {kpi_ranking_total_valor:,.2f}"
    kpis["ranking_preco_total_orcs"] = len(numeros_orcs_preco)

    # Filtra a tabela detalhada se status_filtro foi escolhido
    df_tab = df_visao.drop(columns=["dados_brutos"]).copy()
    if status_filtro and status_filtro in ("FINALIZADO", "PENDENTE", "PERDIDO"):
        df_tab = df_tab[df_tab["status_consolidado"] == status_filtro].copy()

    df_tab["data_emissao_dt"] = pd.to_datetime(df_tab["data_emissao"])
    df_tab = df_tab.sort_values(["data_emissao_dt", "numero_orcamento"], ascending=[False, False])
    df_tab["data_str"] = df_tab["data_emissao_dt"].dt.strftime("%d/%m/%Y").fillna("-")
    df_tab["data_iso"] = df_tab["data_emissao_dt"].dt.strftime("%Y-%m-%d").fillna("")

    tabela_orcamentos = df_tab.to_dict(orient="records")
    return (
        kpis,
        grafico_status_html,
        grafico_vendedores_html,
        grafico_causas_perda_html,
        resumo_vendedores,
        tabela_orcamentos,
        ranking_produtos_preco,
    )


def _classificar_categoria_dre_cp(centro_custo: str, grupo_conta: str, subconta: str) -> str:
    """
    Agrupa um lançamento de Contas a Pagar nas linhas principais de apuração do Lucro Líquido (DRE Simplificado).
    """
    cc = (centro_custo or "").strip().upper()
    gc = (grupo_conta or "").strip().upper()
    sc = (subconta or "").strip().upper()
    texto = f"{cc} | {gc} | {sc}"

    if "COMPRA MERCADORIA" in texto or "MATERIA PRIMA" in texto or cc == "COMPRAS/ESTOQUE":
        return "CUSTO_MERCADORIA"
    if cc == "IMPOSTOS" or "IMPOSTO" in gc or "SIMPLES NACIONAL" in texto or "ICMS" in texto or "DARF" in texto:
        return "IMPOSTOS_TAXAS"
    if cc == "DIRETORIA" or "SOCIOS" in gc or "RETIRADA SOCIO" in texto or "PRO LABORE" in texto:
        return "SOCIOS_DIRETORIA"
    if cc == "VENDAS" or "COMISS" in texto or "FRETE SOBRE VENDAS" in texto or "VENDAS -" in gc:
        return "DESPESAS_VENDAS"
    return "DESPESAS_ADM_OPERACIONAL"


def gerar_dashboard_financeiro(
    mes_selecionado: int,
    ano_selecionado: int,
    empresa_filtro: str = "TODAS",
    regime: str = "CAIXA",
    portador_filtro: str = "TODOS",
    status_filtro: str = "TODOS",
    gerar_graficos: bool = True,
):
    """
    Calcula os indicadores principais da página Financeiro:
    1. Cálculo do Lucro Líquido (DRE Simplificado pelo Regime de Caixa ou Competência/Vencimento)
    2. Conciliação Bancária por Portador/Banco e listagem detalhada de Contas a Receber e Contas a Pagar.
    """
    from .financeiro_services import eh_transferencia_intercompany

    regime_limpo = (regime or "CAIXA").strip().upper()
    if regime_limpo not in ("CAIXA", "COMPETENCIA"):
        regime_limpo = "CAIXA"

    status_limpo = (status_filtro or "TODOS").strip().upper()
    consolidado = (not empresa_filtro) or empresa_filtro == "TODAS"

    # Faturamento Bruto de Notas Fiscais no mês (referência comercial)
    nf_qs = NotaFiscal.objects.exclude(status="CANCELADA").filter(
        data_emissao__year=ano_selecionado,
        data_emissao__month=mes_selecionado,
    )
    if empresa_filtro and empresa_filtro != "TODAS":
        nf_qs = nf_qs.filter(empresa=empresa_filtro)
    faturamento_nf_mes = sum(float(v or 0) for v in nf_qs.values_list("valor_total", flat=True))

    # Base Contas a Receber e Contas a Pagar (excluindo canceladas)
    cr_base = ContaReceber.objects.filter(cancelada=False).exclude(status="CANCELADO")
    cp_base = ContaPagar.objects.filter(cancelada=False).exclude(status="CANCELADO")

    if empresa_filtro and empresa_filtro != "TODAS":
        cr_base = cr_base.filter(empresa=empresa_filtro)
        cp_base = cp_base.filter(empresa=empresa_filtro)

    # Lista de portadores disponíveis para filtro de Conciliação Bancária
    portadores_cr = set(
        p.strip().upper()
        for p in cr_base.exclude(portador__isnull=True).order_by().values_list("portador", flat=True).distinct()
        if p and p.strip()
    )
    portadores_cp = set(
        p.strip().upper()
        for p in cp_base.exclude(portador__isnull=True).order_by().values_list("portador", flat=True).distinct()
        if p and p.strip()
    )
    portadores_disponiveis = sorted(portadores_cr | portadores_cp)

    if portador_filtro and portador_filtro != "TODOS":
        cr_base = cr_base.filter(portador__iexact=portador_filtro)
        cp_base = cp_base.filter(portador__iexact=portador_filtro)

    # No regime CAIXA: seleciona títulos com recebimento/pagamento no mês OU vencimento em aberto no mês (para conciliar)
    # No regime COMPETENCIA: seleciona títulos com vencimento no mês
    from django.db.models import Q

    if regime_limpo == "CAIXA":
        cr_qs = cr_base.filter(
            Q(data_recebimento__year=ano_selecionado, data_recebimento__month=mes_selecionado)
            | Q(data_vencimento__year=ano_selecionado, data_vencimento__month=mes_selecionado)
        )
        cp_qs = cp_base.filter(
            Q(data_pagamento__year=ano_selecionado, data_pagamento__month=mes_selecionado)
            | Q(data_vencimento__year=ano_selecionado, data_vencimento__month=mes_selecionado)
        )
    else:
        cr_qs = cr_base.filter(
            data_vencimento__year=ano_selecionado,
            data_vencimento__month=mes_selecionado,
        )
        cp_qs = cp_base.filter(
            data_vencimento__year=ano_selecionado,
            data_vencimento__month=mes_selecionado,
        )

    cr_list = list(
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
    cp_list = list(
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

    # Processamento Contas a Receber
    entradas_realizadas_caixa = 0.0
    entradas_previstas_venc = 0.0
    saldo_a_receber_mes = 0.0
    saldo_vencido_receber_mes = 0.0
    qtd_cr_recebidos = 0
    qtd_cr_pendentes = 0

    conciliacao_portador_map = {}

    def _garantir_portador(nome_p):
        chave = (nome_p or "NÃO INFORMADO").strip().upper() or "NÃO INFORMADO"
        if chave not in conciliacao_portador_map:
            conciliacao_portador_map[chave] = {
                "portador": chave,
                "qtd_cr_baixados": 0,
                "entradas_recebidas": 0.0,
                "entradas_a_receber": 0.0,
                "qtd_cp_baixados": 0,
                "saidas_pagas": 0.0,
                "saidas_a_pagar": 0.0,
                "saldo_liquido_conciliado": 0.0,
            }
        return conciliacao_portador_map[chave]

    tabela_cr = []
    for item in cr_list:
        if consolidado and eh_transferencia_intercompany(
            nome_parte=item.get("cliente_nome"),
            documento_parte=item.get("cliente_documento"),
            grupo_conta=item.get("grupo_conta"),
            subconta=item.get("subconta"),
            observacao=item.get("observacao"),
        ):
            continue

        st = item["status"]
        if status_limpo == "A_VENCER" and st != "A_VENCER":
            continue
        elif status_limpo == "VENCIDO" and st != "VENCIDO":
            continue
        elif status_limpo in ("RECEBIDO", "REALIZADO") and st != "RECEBIDO":
            continue

        v_tot = float(item["valor_total"] or 0.0)
        v_rec = float(item["valor_recebido"] or 0.0)
        v_sal = float(item["valor_saldo"] or 0.0)
        dt_venc = item["data_vencimento"]
        dt_rec = item["data_recebimento"]

        venc_no_mes = bool(dt_venc and dt_venc.year == ano_selecionado and dt_venc.month == mes_selecionado)
        rec_no_mes = bool(dt_rec and dt_rec.year == ano_selecionado and dt_rec.month == mes_selecionado)

        val_efetivo_rec = v_rec if v_rec > 0 else (v_tot if st == "RECEBIDO" and v_sal <= 0 else 0.0)
        p_info = _garantir_portador(item.get("portador"))

        if venc_no_mes:
            entradas_previstas_venc += v_tot
            if st != "RECEBIDO":
                saldo_aberto = v_sal if v_sal > 0 else v_tot
                saldo_a_receber_mes += saldo_aberto
                p_info["entradas_a_receber"] += saldo_aberto
                qtd_cr_pendentes += 1
                if st == "VENCIDO":
                    saldo_vencido_receber_mes += saldo_aberto

        if (regime_limpo == "CAIXA" and rec_no_mes and st == "RECEBIDO") or (
            regime_limpo == "COMPETENCIA" and venc_no_mes and st == "RECEBIDO"
        ):
            entradas_realizadas_caixa += val_efetivo_rec
            qtd_cr_recebidos += 1
            p_info["qtd_cr_baixados"] += 1
            p_info["entradas_recebidas"] += val_efetivo_rec

        # Filtra o que exibir na tabela conforme o regime selecionado
        if regime_limpo == "CAIXA" and not (rec_no_mes or (venc_no_mes and st != "RECEBIDO")):
            continue

        tabela_cr.append(
            {
                "id": item["id"],
                "id_titulo_erp": item["id_titulo_erp"],
                "empresa": item["empresa"],
                "documento": item["numero_duplicata"] or item["numero_documento"] or item["id_titulo_erp"],
                "parcela": item["parcela"] or "-",
                "cliente_nome": item["cliente_nome"] or "CLIENTE NÃO IDENTIFICADO",
                "portador": (item["portador"] or "NÃO INFORMADO").strip().upper(),
                "nosso_numero": item["nosso_numero"] or "-",
                "data_venc_str": dt_venc.strftime("%d/%m/%Y") if dt_venc else "-",
                "data_venc_iso": dt_venc.strftime("%Y-%m-%d") if dt_venc else "",
                "data_rec_str": dt_rec.strftime("%d/%m/%Y") if dt_rec else "-",
                "data_rec_iso": dt_rec.strftime("%Y-%m-%d") if dt_rec else "",
                "valor_total": v_tot,
                "valor_recebido": val_efetivo_rec if st == "RECEBIDO" else v_rec,
                "valor_saldo": 0.0 if st == "RECEBIDO" else (v_sal if v_sal > 0 else v_tot),
                "status": st,
            }
        )

    # Processamento Contas a Pagar e DRE Simplificado
    saidas_realizadas_caixa = 0.0
    saidas_previstas_venc = 0.0
    saldo_a_pagar_mes = 0.0
    saldo_vencido_pagar_mes = 0.0
    qtd_cp_pagos = 0
    qtd_cp_pendentes = 0

    dre_categorias = {
        "CUSTO_MERCADORIA": 0.0,
        "IMPOSTOS_TAXAS": 0.0,
        "DESPESAS_VENDAS": 0.0,
        "DESPESAS_ADM_OPERACIONAL": 0.0,
        "SOCIOS_DIRETORIA": 0.0,
    }

    tabela_cp = []
    for item in cp_list:
        if consolidado and eh_transferencia_intercompany(
            nome_parte=item.get("fornecedor_nome"),
            documento_parte=item.get("fornecedor_documento"),
            grupo_conta=item.get("grupo_conta"),
            subconta=item.get("subconta"),
            observacao=item.get("observacao"),
        ):
            continue

        st = item["status"]
        if status_limpo == "A_VENCER" and st != "A_VENCER":
            continue
        elif status_limpo == "VENCIDO" and st != "VENCIDO":
            continue
        elif status_limpo in ("RECEBIDO", "REALIZADO", "PAGO") and st != "PAGO":
            continue

        v_tot = float(item["valor_total"] or 0.0)
        v_pag = float(item["valor_pago"] or 0.0)
        v_sal = float(item["valor_saldo"] or 0.0)
        dt_venc = item["data_vencimento"]
        dt_pag = item["data_pagamento"]

        venc_no_mes = bool(dt_venc and dt_venc.year == ano_selecionado and dt_venc.month == mes_selecionado)
        pag_no_mes = bool(dt_pag and dt_pag.year == ano_selecionado and dt_pag.month == mes_selecionado)

        val_efetivo_pag = v_pag if v_pag > 0 else (v_tot if st == "PAGO" and v_sal <= 0 else 0.0)
        cat_dre = _classificar_categoria_dre_cp(item.get("centro_custo"), item.get("grupo_conta"), item.get("subconta"))
        p_info = _garantir_portador(item.get("portador"))

        if venc_no_mes:
            saidas_previstas_venc += v_tot
            if st != "PAGO":
                saldo_aberto = v_sal if v_sal > 0 else v_tot
                saldo_a_pagar_mes += saldo_aberto
                p_info["saidas_a_pagar"] += saldo_aberto
                qtd_cp_pendentes += 1
                if st == "VENCIDO":
                    saldo_vencido_pagar_mes += saldo_aberto

        # Para apuração do DRE:
        # No regime CAIXA considera o que foi efetivamente PAGO no mês.
        # No regime COMPETENCIA considera todos os títulos com vencimento no mês (valor_total).
        if regime_limpo == "CAIXA":
            if pag_no_mes and st == "PAGO":
                saidas_realizadas_caixa += val_efetivo_pag
                qtd_cp_pagos += 1
                p_info["qtd_cp_baixados"] += 1
                p_info["saidas_pagas"] += val_efetivo_pag
                dre_categorias[cat_dre] += val_efetivo_pag
        else:
            if venc_no_mes:
                dre_categorias[cat_dre] += v_tot
                if st == "PAGO":
                    saidas_realizadas_caixa += val_efetivo_pag
                    qtd_cp_pagos += 1
                    p_info["qtd_cp_baixados"] += 1
                    p_info["saidas_pagas"] += val_efetivo_pag

        if regime_limpo == "CAIXA" and not (pag_no_mes or (venc_no_mes and st != "PAGO")):
            continue

        tabela_cp.append(
            {
                "id": item["id"],
                "id_titulo_erp": item["id_titulo_erp"],
                "empresa": item["empresa"],
                "documento": item["numero_duplicata"] or item["numero_documento"] or item["id_titulo_erp"],
                "parcela": item["parcela"] or "-",
                "fornecedor_nome": item["fornecedor_nome"] or "FORNECEDOR NÃO IDENTIFICADO",
                "grupo_conta": item["grupo_conta"] or item["subconta"] or item["centro_custo"] or "-",
                "centro_custo": item["centro_custo"] or "-",
                "portador": (item["portador"] or "NÃO INFORMADO").strip().upper(),
                "data_venc_str": dt_venc.strftime("%d/%m/%Y") if dt_venc else "-",
                "data_venc_iso": dt_venc.strftime("%Y-%m-%d") if dt_venc else "",
                "data_pag_str": dt_pag.strftime("%d/%m/%Y") if dt_pag else "-",
                "data_pag_iso": dt_pag.strftime("%Y-%m-%d") if dt_pag else "",
                "valor_total": v_tot,
                "valor_pago": val_efetivo_pag if st == "PAGO" else max(v_pag, 0.0),
                "valor_saldo": 0.0 if st == "PAGO" else (v_sal if v_sal > 0 else v_tot),
                "status": st,
            }
        )

    # Consolida Conciliação por Portador / Forma de Pagamento
    resumo_portadores = []
    soma_rec_port = sum(p["entradas_recebidas"] for p in conciliacao_portador_map.values())
    soma_pag_port = sum(p["saidas_pagas"] for p in conciliacao_portador_map.values())

    for p_data in conciliacao_portador_map.values():
        p_data["saldo_liquido_conciliado"] = p_data["entradas_recebidas"] - p_data["saidas_pagas"]
        if regime_limpo == "CAIXA":
            p_data["valor_entrada_ref"] = (
                p_data["entradas_recebidas"]
                if soma_rec_port > 0
                else (p_data["entradas_recebidas"] + p_data["entradas_a_receber"])
            )
            p_data["valor_saida_ref"] = (
                p_data["saidas_pagas"]
                if soma_pag_port > 0
                else (p_data["saidas_pagas"] + p_data["saidas_a_pagar"])
            )
        else:
            p_data["valor_entrada_ref"] = p_data["entradas_recebidas"] + p_data["entradas_a_receber"]
            p_data["valor_saida_ref"] = p_data["saidas_pagas"] + p_data["saidas_a_pagar"]

        if (
            p_data["entradas_recebidas"] > 0
            or p_data["saidas_pagas"] > 0
            or p_data["entradas_a_receber"] > 0
            or p_data["saidas_a_pagar"] > 0
        ):
            resumo_portadores.append(p_data)

    total_entradas_port = sum(p["valor_entrada_ref"] for p in resumo_portadores) or 1.0
    total_saidas_port = sum(p["valor_saida_ref"] for p in resumo_portadores) or 1.0
    for p_data in resumo_portadores:
        p_data["pct_entradas"] = round(p_data["valor_entrada_ref"] / total_entradas_port * 100.0, 2)
        p_data["pct_saidas"] = round(p_data["valor_saida_ref"] / total_saidas_port * 100.0, 2)

    resumo_portadores.sort(
        key=lambda x: (x["entradas_recebidas"] + x["saidas_pagas"] + x["entradas_a_receber"] + x["saidas_a_pagar"]),
        reverse=True,
    )

    # Cálculo do DRE Simplificado (Lucro Líquido)
    receita_base_dre = entradas_realizadas_caixa if regime_limpo == "CAIXA" else entradas_previstas_venc
    saidas_base_dre = sum(dre_categorias.values())

    custo_mercadoria = dre_categorias["CUSTO_MERCADORIA"]
    impostos_taxas = dre_categorias["IMPOSTOS_TAXAS"]
    despesas_vendas = dre_categorias["DESPESAS_VENDAS"]
    despesas_adm = dre_categorias["DESPESAS_ADM_OPERACIONAL"]
    socios_diretoria = dre_categorias["SOCIOS_DIRETORIA"]

    margem_bruta_fin = receita_base_dre - custo_mercadoria
    lucro_operacional = margem_bruta_fin - impostos_taxas - despesas_vendas - despesas_adm
    lucro_liquido_final = lucro_operacional - socios_diretoria

    margem_operacional_pct = (lucro_operacional / receita_base_dre * 100.0) if receita_base_dre > 0 else 0.0
    margem_liquida_pct = (lucro_liquido_final / receita_base_dre * 100.0) if receita_base_dre > 0 else 0.0

    dre = {
        "regime": regime_limpo,
        "faturamento_nf": f"R$ {faturamento_nf_mes:,.2f}",
        "receita_base": f"R$ {receita_base_dre:,.2f}",
        "receita_base_raw": receita_base_dre,
        "custo_mercadoria": f"R$ {custo_mercadoria:,.2f}",
        "margem_bruta": f"R$ {margem_bruta_fin:,.2f}",
        "margem_bruta_positiva": margem_bruta_fin >= 0,
        "impostos_taxas": f"R$ {impostos_taxas:,.2f}",
        "despesas_vendas": f"R$ {despesas_vendas:,.2f}",
        "despesas_adm": f"R$ {despesas_adm:,.2f}",
        "lucro_operacional": f"R$ {lucro_operacional:,.2f}",
        "lucro_operacional_raw": lucro_operacional,
        "lucro_operacional_positivo": lucro_operacional >= 0,
        "margem_operacional_pct": f"{margem_operacional_pct:.1f}%",
        "socios_diretoria": f"R$ {socios_diretoria:,.2f}",
        "saidas_totais": f"R$ {saidas_base_dre:,.2f}",
        "saidas_totais_raw": saidas_base_dre,
        "lucro_liquido": f"R$ {lucro_liquido_final:,.2f}",
        "lucro_liquido_raw": lucro_liquido_final,
        "lucro_liquido_positivo": lucro_liquido_final >= 0,
        "margem_liquida_pct": f"{margem_liquida_pct:.1f}%",
        "grafico_pizza_entradas": "",
        "grafico_pizza_saidas": "",
    }

    kpis = {
        "entradas_recebidas": f"R$ {entradas_realizadas_caixa:,.2f}",
        "entradas_previstas": f"R$ {entradas_previstas_venc:,.2f}",
        "saldo_a_receber": f"R$ {saldo_a_receber_mes:,.2f}",
        "saldo_vencido_receber": f"R$ {saldo_vencido_receber_mes:,.2f}",
        "qtd_cr_recebidos": qtd_cr_recebidos,
        "qtd_cr_pendentes": qtd_cr_pendentes,
        "saidas_pagas": f"R$ {saidas_realizadas_caixa:,.2f}",
        "saidas_previstas": f"R$ {saidas_previstas_venc:,.2f}",
        "saldo_a_pagar": f"R$ {saldo_a_pagar_mes:,.2f}",
        "saldo_vencido_pagar": f"R$ {saldo_vencido_pagar_mes:,.2f}",
        "qtd_cp_pagos": qtd_cp_pagos,
        "qtd_cp_pendentes": qtd_cp_pendentes,
        "lucro_liquido": dre["lucro_liquido"],
        "lucro_liquido_positivo": dre["lucro_liquido_positivo"],
        "margem_liquida_pct": dre["margem_liquida_pct"],
        "lucro_operacional": dre["lucro_operacional"],
        "margem_operacional_pct": dre["margem_operacional_pct"],
    }

    grafico_dre_html = ""
    if gerar_graficos:
        # 1. Gráfico de Cascatas (Waterfall) da Composição do Resultado & Lucro Líquido
        fig_dre = go.Figure(
            go.Waterfall(
                name="Resultado",
                orientation="v",
                measure=["absolute", "relative", "relative", "relative", "relative", "relative", "total"],
                x=[
                    "Entradas / Receitas",
                    "Fornecedores (Mercadoria)",
                    "Impostos & Taxas",
                    "Vendas & Folha",
                    "Adm. & Operacional",
                    "Sócios / Diretoria",
                    "Lucro Líquido",
                ],
                textposition="outside",
                text=[
                    f"R$ {receita_base_dre:,.0f}",
                    f"-R$ {custo_mercadoria:,.0f}" if custo_mercadoria > 0 else "R$ 0",
                    f"-R$ {impostos_taxas:,.0f}" if impostos_taxas > 0 else "R$ 0",
                    f"-R$ {despesas_vendas:,.0f}" if despesas_vendas > 0 else "R$ 0",
                    f"-R$ {despesas_adm:,.0f}" if despesas_adm > 0 else "R$ 0",
                    f"-R$ {socios_diretoria:,.0f}" if socios_diretoria > 0 else "R$ 0",
                    f"R$ {lucro_liquido_final:,.0f}",
                ],
                y=[
                    receita_base_dre,
                    -custo_mercadoria,
                    -impostos_taxas,
                    -despesas_vendas,
                    -despesas_adm,
                    -socios_diretoria,
                    lucro_liquido_final,
                ],
                connector={"line": {"color": "#64748b", "width": 1.5, "dash": "dot"}},
                increasing={"marker": {"color": "#15803d"}},
                decreasing={"marker": {"color": "#dc2626"}},
                totals={"marker": {"color": "#0284c7" if lucro_liquido_final >= 0 else "#b91c1c"}},
            )
        )
        fig_dre.update_layout(
            title=f"💰 Composição do Resultado & Lucro Líquido ({mes_selecionado:02d}/{ano_selecionado} - {regime_limpo})",
            showlegend=False,
            height=360,
            margin=dict(t=50, b=30, l=25, r=20),
            yaxis_title="Valor (R$)",
        )
        grafico_dre_html = fig_to_html(fig_dre)

        # 2. Dois Gráficos de Pizza: % de Entradas e % de Saídas por Portador / Forma de Pagamento
        dados_in_port = [p for p in resumo_portadores if p["valor_entrada_ref"] > 0]
        if dados_in_port:
            df_pie_in = pd.DataFrame(dados_in_port)
            fig_pie_in = px.pie(
                df_pie_in,
                names="portador",
                values="valor_entrada_ref",
                hole=0.38,
                title="🟢 % de Entradas por Portador / Forma de Pagamento",
                color_discrete_sequence=px.colors.qualitative.Set2,
            )
            fig_pie_in.update_traces(
                textposition="inside",
                textinfo="percent+label",
                hovertemplate="<b>%{label}</b><br>Valor: R$ %{value:,.2f}<br>Participação: %{percent}<extra></extra>",
            )
            fig_pie_in.update_layout(
                height=340,
                margin=dict(t=45, b=20, l=15, r=15),
                legend=dict(orientation="h", yanchor="top", y=-0.05, xanchor="center", x=0.5),
            )
            dre["grafico_pizza_entradas"] = fig_to_html(fig_pie_in)

        dados_out_port = [p for p in resumo_portadores if p["valor_saida_ref"] > 0]
        if dados_out_port:
            df_pie_out = pd.DataFrame(dados_out_port)
            fig_pie_out = px.pie(
                df_pie_out,
                names="portador",
                values="valor_saida_ref",
                hole=0.38,
                title="🔴 % de Saídas por Portador / Forma de Pagamento",
                color_discrete_sequence=px.colors.qualitative.Pastel1,
            )
            fig_pie_out.update_traces(
                textposition="inside",
                textinfo="percent+label",
                hovertemplate="<b>%{label}</b><br>Valor: R$ %{value:,.2f}<br>Participação: %{percent}<extra></extra>",
            )
            fig_pie_out.update_layout(
                height=340,
                margin=dict(t=45, b=20, l=15, r=15),
                legend=dict(orientation="h", yanchor="top", y=-0.05, xanchor="center", x=0.5),
            )
            dre["grafico_pizza_saidas"] = fig_to_html(fig_pie_out)

    tabela_cr.sort(key=lambda r: (r["data_rec_iso"] or r["data_venc_iso"], r["documento"]), reverse=True)
    tabela_cp.sort(key=lambda r: (r["data_pag_iso"] or r["data_venc_iso"], r["documento"]), reverse=True)

    return (
        kpis,
        dre,
        grafico_dre_html,
        resumo_portadores,
        tabela_cr,
        tabela_cp,
        portadores_disponiveis,
    )