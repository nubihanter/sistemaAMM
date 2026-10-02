import builtins as _builtins
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from decimal import Decimal
import pandas as pd
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from integrations.ca_epi import ConsultaCAClient
from integrations.hardness import HardnessAPI
from sales.models import NotaFiscal
from .models import Categoria, CertificadoAprovacao, ProdutoEPI, ItemVenda


def print(*args, **kwargs):
    """Wrapper seguro de print para evitar UnicodeEncodeError em consoles Windows (cp1252)."""
    try:
        _builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        texto = " ".join(str(a) for a in args)
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        texto_seguro = texto.encode(enc, errors="replace").decode(enc, errors="replace")
        _builtins.print(texto_seguro, **kwargs)


def parse_decimal(valor, default="0.00"):
    if valor is None or pd.isna(valor) or str(valor).strip() in ("", "-", "None", "nan"):
        return Decimal(default)
    if isinstance(valor, (int, float, Decimal)):
        try:
            return Decimal(str(valor))
        except Exception:
            return Decimal(default)

    val_str = str(valor).replace("R$", "").strip()
    if "." in val_str and "," in val_str:
        if val_str.rfind(",") > val_str.rfind("."):
            val_str = val_str.replace(".", "").replace(",", ".")
        else:
            val_str = val_str.replace(",", "")
    elif "," in val_str:
        val_str = val_str.replace(",", ".")

    try:
        return Decimal(val_str)
    except Exception:
        return Decimal(default)


def parse_int(valor, default=0):
    try:
        dec = parse_decimal(valor, default=str(default))
        return int(round(float(dec)))
    except Exception:
        return default


def parse_data(data_val):
    if not data_val or pd.isna(data_val):
        return None
    if isinstance(data_val, (date, datetime)):
        return data_val if isinstance(data_val, date) else data_val.date()

    data_str = str(data_val).strip()[:10]
    if not data_str or data_str in ("0000-00-00", "None", "nan", "-"):
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(data_str, fmt).date()
        except Exception:
            continue
    return None


def extrair_numero_ca(descricao: str):
    """Extrai o número do CA (ex: 'CA:47898', 'CA: 46.381', 'C.A 43319') da descrição."""
    if not descricao:
        return None
    match = re.search(r"\bC\.?A\.?\s*:?\s*([\d\.]{3,9})\b", str(descricao), re.IGNORECASE)
    if match:
        ca_limpo = match.group(1).replace(".", "").strip()
        if ca_limpo.isdigit() and len(ca_limpo) >= 3:
            return ca_limpo
    return None


def mapear_unidade(un_raw: str) -> str:
    un = str(un_raw or "UN").strip().upper()
    mapa = {
        "PR": "PAR",
        "PAR": "PAR",
        "PC": "PC",
        "PÇ": "PC",
        "UN": "UN",
        "UND": "UN",
        "CJ": "CJ",
        "CONJ": "CJ",
        "CX": "CX",
        "PCT": "PCT",
        "PT": "PCT",
        "RL": "RL",
        "KG": "KG",
        "MT": "MT",
        "M": "MT",
    }
    return mapa.get(un, "UN")


def sincronizar_estoque_hardness():
    """
    Sincroniza a base completa de produtos e saldos de estoque do Hardness ERP
    diretamente na tabela ProdutoEPI (preservando códigos com variações/tamanhos,
    estoque mínimo e flags de item crítico definidos pelo usuário).
    """
    api = HardnessAPI(verbose=True)
    api.login()
    api.trocar_empresa("1")

    if not api.filtrar_estoque(codigo="!"):
        print("⚠️ Falha ao aplicar filtro de estoque no Hardness.")
        return 0, 0

    df_estoque = api.get_dados_estoque()
    if df_estoque.empty:
        print("ℹ️ Nenhum item retornado da grade de estoque do Hardness.")
        return 0, 0

    col_liq_fora = "D009A_Qtd_Liquida_Fora\t+ D009_Quantidade_Estoque_Liquido"
    col_fis_fora = "(D009_Quantidade_Estoque) + (D009A_Qtd_Fisica_Fora)"

    # Cache em memória para categorias e CAs
    categorias_cache = {c.nome.upper(): c for c in Categoria.objects.all()}
    cas_cache = {ca.numero_ca: ca for ca in CertificadoAprovacao.objects.all()}
    produtos_existentes = {p.sku: p for p in ProdutoEPI.objects.all()}

    criados = 0
    atualizados = 0
    hoje = timezone.now().date()

    with transaction.atomic():
        for _, row in df_estoque.iterrows():
            item = row.to_dict()
            sku = str(item.get("D001_Codigo_Produto") or "").strip()
            if not sku or sku.lower() in ("nan", "none"):
                continue

            nome = str(item.get("D001_Descricao_Produto") or sku).strip()
            marca = str(item.get("D082_Marca") or "").strip().upper()
            if marca in ("NAN", "NONE", ""):
                marca = "N/A"

            nome_linha = str(item.get("D003_Nome_Linha") or item.get("D002_Descricao_Produto") or "GERAL").strip().upper()
            if not nome_linha or nome_linha in ("NAN", "NONE"):
                nome_linha = "GERAL"

            categoria_obj = categorias_cache.get(nome_linha)
            if not categoria_obj:
                categoria_obj, _ = Categoria.objects.get_or_create(
                    nome=nome_linha,
                    defaults={"descricao": f"Linha importada do Hardness: {nome_linha}"}
                )
                categorias_cache[nome_linha] = categoria_obj

            # Extração automática do CA se constar na descrição
            num_ca = extrair_numero_ca(nome)
            ca_obj = None
            if num_ca:
                ca_obj = cas_cache.get(num_ca)
                if not ca_obj:
                    ca_obj, _ = CertificadoAprovacao.objects.get_or_create(
                        numero_ca=num_ca,
                        defaults={
                            "data_validade": None,
                            "status": "VALIDO",
                            "fabricante": marca if marca != "N/A" else "NÃO INFORMADO",
                            "descricao_equipamento": nome,
                            "ultima_consulta_api": None,
                        }
                    )
                    cas_cache[num_ca] = ca_obj

            # Variação / tamanho a partir do sufixo do código (sem truncar o SKU!)
            tamanho_var = None
            if "-" in sku:
                tamanho_var = sku.split("-", 1)[1].strip()

            unidade = mapear_unidade(item.get("D037_Unidade"))
            preco_custo = parse_decimal(item.get("D009_Valor_Custo_Unitario"))
            preco_venda = parse_decimal(item.get("D009_Preco_1"))

            # Cálculo seguro do estoque líquido e físico
            raw_liq = item.get(col_liq_fora)
            raw_fis = item.get(col_fis_fora)
            est_base = parse_int(item.get("D009_Quantidade_Estoque"), 0)
            est_fora = parse_int(item.get("D009_Quantidade_Estoque_Fora"), 0)
            pedido_aberto = parse_int(item.get("Pedido"), 0)

            if raw_liq is not None and not pd.isna(raw_liq) and str(raw_liq).strip() not in ("", "-", "None"):
                estoque_liquido = parse_int(raw_liq, 0)
            else:
                # Fallback quando D009A é NULL no ERP
                estoque_liquido = est_base + est_fora - pedido_aberto

            if raw_fis is not None and not pd.isna(raw_fis) and str(raw_fis).strip() not in ("", "-", "None"):
                estoque_fisico = parse_int(raw_fis, 0)
            else:
                estoque_fisico = est_base + est_fora

            qtd_oc = parse_int(item.get("D009_Quantidade_OC"), 0)
            ativo_erp = str(item.get("D001_Flag_Ativo", "S")).strip().upper() != "N"
            nao_comprar = str(item.get("D049_Flag_Nao_Comprar", "N")).strip().upper() == "S"
            dt_ult_entrada = parse_data(item.get("D009_Data_Ultima_Entrada"))
            dt_ult_saida = parse_data(item.get("D009_Data_Ultima_Venda"))

            prod_existente = produtos_existentes.get(sku)
            if prod_existente:
                if not prod_existente.editado_manualmente:
                    prod_existente.nome = nome
                    prod_existente.marca = marca
                    prod_existente.categoria = categoria_obj
                    if ca_obj and not prod_existente.ca_id:
                        prod_existente.ca = ca_obj
                    if tamanho_var and not prod_existente.tamanho_variacao:
                        prod_existente.tamanho_variacao = tamanho_var
                prod_existente.unidade_medida = unidade
                if preco_custo > 0:
                    prod_existente.preco_custo = preco_custo
                if preco_venda > 0:
                    prod_existente.preco_venda = preco_venda
                prod_existente.estoque_atual = estoque_liquido
                prod_existente.estoque_fisico = estoque_fisico
                prod_existente.qtd_ordem_compra = qtd_oc
                prod_existente.ativo = ativo_erp
                prod_existente.nao_comprar_erp = nao_comprar
                if dt_ult_entrada:
                    prod_existente.data_ultima_entrada = dt_ult_entrada
                if dt_ult_saida:
                    prod_existente.data_ultima_saida = dt_ult_saida
                prod_existente.save()
                atualizados += 1
            else:
                novo_prod = ProdutoEPI.objects.create(
                    sku=sku,
                    nome=nome,
                    marca=marca,
                    categoria=categoria_obj,
                    ca=ca_obj,
                    tamanho_variacao=tamanho_var,
                    unidade_medida=unidade,
                    preco_custo=preco_custo,
                    preco_venda=preco_venda,
                    estoque_atual=estoque_liquido,
                    estoque_fisico=estoque_fisico,
                    qtd_ordem_compra=qtd_oc,
                    estoque_minimo=0,
                    estoque_maximo=0,
                    item_critico=False,
                    nao_comprar_erp=nao_comprar,
                    data_ultima_entrada=dt_ult_entrada,
                    data_ultima_saida=dt_ult_saida,
                    ativo=ativo_erp,
                )
                produtos_existentes[sku] = novo_prod
                criados += 1

    print(f"✅ Estoque sincronizado: {criados} criados | {atualizados} atualizados.")
    return criados, atualizados


def sincronizar_itens_venda_hardness(data_inicio="", data_fim="", empresa_nome=None):
    """
    Sincroniza os itens vendidos por Nota Fiscal (produtos_url do Hardness)
    e vincula cada linha ao ProdutoEPI correspondente e ao cliente da NotaFiscal.
    """
    api = HardnessAPI(verbose=True)
    api.login()

    if empresa_nome and empresa_nome.upper() != "TODAS":
        empresas_alvo = {empresa_nome: api.empresas_dict.get(empresa_nome, {"id_sistema": "1"})}
    else:
        empresas_alvo = api.empresas_dict

    # Mapa de NotaFiscal -> cliente_nome completo para enriquecer o nome do cliente sem truncamento
    mapa_nf_empresa_cliente = {}
    mapa_nf_cliente = {}
    mapa_doc_cliente = {}
    for nf in NotaFiscal.objects.values("empresa", "numero_nota", "cliente_nome", "cliente_documento"):
        if nf["numero_nota"] and nf["cliente_nome"]:
            num_limpo = str(nf["numero_nota"]).strip()
            cli_limpo = nf["cliente_nome"].strip()
            emp_limpa = str(nf.get("empresa") or "").strip()
            mapa_nf_empresa_cliente[(emp_limpa, num_limpo)] = cli_limpo
            mapa_nf_cliente[num_limpo] = cli_limpo
        if nf["cliente_documento"] and nf["cliente_nome"]:
            mapa_doc_cliente[str(nf["cliente_documento"]).strip()] = nf["cliente_nome"].strip()

    produtos_map = {p.sku: p for p in ProdutoEPI.objects.all()}

    total_criados = 0
    total_atualizados = 0

    for nome_empresa, config_empresa in empresas_alvo.items():
        empresa_id = config_empresa.get("id_sistema")
        print(f"\n==================================================")
        print(f"📦 Sincronizando Itens de Venda: {nome_empresa} (ID: {empresa_id})")
        print(f"==================================================")

        if not api.trocar_empresa(empresa_id):
            print(f"⚠️ Pulando {nome_empresa} devido a erro na troca de sessão.")
            continue

        filtro_ok = api.filtrar(
            data_inicio=data_inicio,
            data_fim=data_fim,
            CFOP="VENDA",
            cancelada="",
            url=api.produtos_url
        )
        if not filtro_ok:
            print(f"⚠️ Não foi possível aplicar o filtro de produtos em {nome_empresa}.")
            continue

        df_itens = api.get_dados(url=api.produtos_url)
        if df_itens.empty:
            print(f"ℹ️ Nenhum item de venda encontrado para {nome_empresa} no período.")
            continue

        itens_preparados = {}
        for item in df_itens.to_dict("records"):
            cancelada = str(item.get("T007_Flag_Cancelada", "N")).strip().upper()
            if cancelada == "S":
                t008_id = str(item.get("T008_Id") or "").strip()
                id_item_erp = f"{empresa_id}-{t008_id}"
                numero_nf = str(item.get("T007_Numero_Nota_Fiscal") or "").strip()
                ItemVenda.objects.filter(Q(id_item_erp=id_item_erp) | Q(empresa=nome_empresa, numero_nota=numero_nf)).delete()
                continue

            t008_id = str(item.get("T008_Id") or "").strip()
            if not t008_id or t008_id.lower() == "nan":
                continue

            id_item_erp = f"{empresa_id}-{t008_id}"
            codigo_prod = str(item.get("T008_Codigo_Produto") or "").strip()
            if not codigo_prod or codigo_prod.lower() == "nan":
                continue

            qtd = parse_decimal(item.get("T008_Quantidade"))
            if qtd <= 0:
                continue

            numero_nf = str(item.get("T007_Numero_Nota_Fiscal") or "").strip()
            if not numero_nf or numero_nf.lower() == "nan":
                numero_nf = f"ID-{item.get('T008_T007_Id', t008_id)}"

            dt_emissao = parse_data(item.get("T007_Data_Emissao"))
            if not dt_emissao:
                continue

            d024_id = str(item.get("D024_Id") or "").strip()
            cliente_nome = (
                mapa_nf_empresa_cliente.get((nome_empresa, numero_nf))
                or mapa_nf_cliente.get(numero_nf)
                or mapa_doc_cliente.get(d024_id)
                or str(item.get("D024_Nome_Fantasia") or item.get("substr(D024_Nome_Empresa,1,20)") or "CLIENTE NÃO IDENTIFICADO").strip()
            )

            vendedor_nome = str(item.get("vendedor.C007_Primeiro_Nome") or "DESCONHECIDO").strip().upper()
            descricao_prod = str(item.get("T008_Descricao_Produto") or codigo_prod).strip()
            marca = str(item.get("substr(D082_Marca,1,12)") or "").strip().upper()
            unidade = str(item.get("D037_Unidade") or "UN").strip().upper()
            cfop = str(item.get("D006_Codigo_CFOP") or "").strip()

            val_unit = parse_decimal(item.get("T008_Valor_Preco_Sem_Desconto_Unitario"))
            val_custo = parse_decimal(item.get("T008_Valor_Custo_Unitario"))
            val_total = parse_decimal(item.get("T008_Valor_Total_Preco_Sem_Desconto") or item.get("Total_Valor_Total"))

            produto_obj = produtos_map.get(codigo_prod)

            itens_preparados[id_item_erp] = {
                "empresa": nome_empresa,
                "numero_nota": numero_nf,
                "data_emissao": dt_emissao,
                "cliente_nome": cliente_nome,
                "cliente_id_erp": d024_id,
                "vendedor_nome": vendedor_nome,
                "produto": produto_obj,
                "codigo_produto": codigo_prod,
                "descricao_produto": descricao_prod,
                "marca": marca,
                "unidade": unidade,
                "cfop": cfop,
                "quantidade": qtd,
                "valor_unitario": val_unit,
                "valor_custo_unitario": val_custo,
                "valor_total": val_total,
            }

        existentes_map = {}
        chaves_lote = list(itens_preparados.keys())
        for i in range(0, len(chaves_lote), 900):
            fatia_ids = chaves_lote[i : i + 900]
            for obj_it in ItemVenda.objects.filter(id_item_erp__in=fatia_ids):
                existentes_map[obj_it.id_item_erp] = obj_it

        agora = timezone.now()
        para_criar = []
        para_atualizar = []
        campos_update = [
            "empresa",
            "numero_nota",
            "data_emissao",
            "cliente_nome",
            "cliente_id_erp",
            "vendedor_nome",
            "produto",
            "codigo_produto",
            "descricao_produto",
            "marca",
            "unidade",
            "cfop",
            "quantidade",
            "valor_unitario",
            "valor_custo_unitario",
            "valor_total",
            "data_sincronizacao",
        ]

        for id_item_erp, defaults in itens_preparados.items():
            it_existente = existentes_map.get(id_item_erp)
            if it_existente:
                for campo, valor in defaults.items():
                    setattr(it_existente, campo, valor)
                it_existente.data_sincronizacao = agora
                para_atualizar.append(it_existente)
            else:
                para_criar.append(ItemVenda(id_item_erp=id_item_erp, **defaults))

        with transaction.atomic():
            if para_criar:
                ItemVenda.objects.bulk_create(para_criar, batch_size=500)
            if para_atualizar:
                ItemVenda.objects.bulk_update(para_atualizar, fields=campos_update, batch_size=500)

        criados_emp = len(para_criar)
        atualizados_emp = len(para_atualizar)

        print(f"✅ {nome_empresa}: {criados_emp} itens criados, {atualizados_emp} atualizados.")
        total_criados += criados_emp
        total_atualizados += atualizados_emp

    # Remove itens de venda associados a notas fiscais canceladas
    nfs_canceladas = set(NotaFiscal.objects.filter(status="CANCELADA").values_list("empresa", "numero_nota"))
    for emp, num_nf in nfs_canceladas:
        num_limpo = num_nf.lstrip("0") or "0"
        vars_nf = {num_nf, num_limpo, num_nf.zfill(7)}
        ItemVenda.objects.filter(empresa=emp, numero_nota__in=vars_nf).delete()

    return total_criados, total_atualizados


def sincronizar_estoque_e_itens_rapido(dias_retroativos_padrao=180):
    """
    Sincroniza o estoque completo e os itens de venda desde a última data gravada
    (ou últimos N dias caso a tabela ItemVenda esteja vazia).
    """
    est_criados, est_atualizados = sincronizar_estoque_hardness()

    ultima_data = ItemVenda.objects.aggregate(Max("data_emissao"))["data_emissao__max"]
    hoje = timezone.now().date()

    if ultima_data:
        # Volta 15 dias por segurança para pegar alterações e cancelamentos recentes
        dt_inicio = ultima_data - timedelta(days=15)
    else:
        dt_inicio = hoje - timedelta(days=dias_retroativos_padrao)

    data_inicio_str = dt_inicio.strftime("%d/%m/%Y")
    data_fim_str = hoje.strftime("%d/%m/%Y")
    print(f"📅 Sincronizando itens vendidos de {data_inicio_str} até {data_fim_str}...")

    itens_criados, itens_atualizados = sincronizar_itens_venda_hardness(
        data_inicio=data_inicio_str,
        data_fim=data_fim_str,
    )

    # Consulta automática apenas dos CAs recém-importados que ainda não têm data_validade
    res_ca = sincronizar_vencimentos_ca(forcar_todos=False)

    return {
        "estoque_criados": est_criados,
        "estoque_atualizados": est_atualizados,
        "itens_criados": itens_criados,
        "itens_atualizados": itens_atualizados,
        "ca_atualizados": res_ca.get("atualizados", 0),
    }


def sincronizar_vencimentos_ca(
    forcar_todos: bool = False,
    numero_ca_especifico: str = None,
    max_workers: int = 6,
) -> dict:
    """
    Consulta e atualiza as datas de vencimento e status dos CAs cadastrados no model CertificadoAprovacao.
    - Por padrão (forcar_todos=False), consulta SOMENTE os CAs que ainda não possuem data_validade cadastrada.
    - Quando forcar_todos=True, força a atualização geral de todos os CAs do banco.
    - Quando numero_ca_especifico é informado, consulta/atualiza apenas aquele CA específico.
    """
    if numero_ca_especifico:
        ca_limpo = ConsultaCAClient.limpar_numero_ca(numero_ca_especifico)
        ca_obj, _ = CertificadoAprovacao.objects.get_or_create(
            numero_ca=ca_limpo,
            defaults={
                "data_validade": None,
                "status": "VALIDO",
                "fabricante": "NÃO INFORMADO",
            },
        )
        cas_alvo = [ca_obj]
    elif forcar_todos:
        cas_alvo = list(CertificadoAprovacao.objects.all().order_by("numero_ca"))
    else:
        cas_alvo = list(
            CertificadoAprovacao.objects.filter(data_validade__isnull=True).order_by("numero_ca")
        )

    total = len(cas_alvo)
    if total == 0:
        print("ℹ️ Nenhum CA pendente de consulta de vencimento.")
        return {"total": 0, "atualizados": 0, "vencidos": 0, "nao_encontrados": 0}

    modo_str = "ATUALIZAÇÃO GERAL (FORÇADA)" if forcar_todos else "APENAS SEM VENCIMENTO CADASTRADO"
    if numero_ca_especifico:
        modo_str = f"CA ESPECÍFICO ({numero_ca_especifico})"
    print(f"🔎 Consultando vencimento de {total} CA(s) [{modo_str}]...")

    atualizados = 0
    vencidos = 0
    nao_encontrados = 0
    agora = timezone.now()

    def _consultar_um(ca_item):
        client = ConsultaCAClient(timeout=15)
        return ca_item, client.consultar_ca(ca_item.numero_ca)

    resultados = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futuros = {executor.submit(_consultar_um, ca_obj): ca_obj for ca_obj in cas_alvo}
        for idx, future in enumerate(as_completed(futuros), start=1):
            ca_obj, dados = future.result()
            resultados.append((ca_obj, dados))
            if idx % 25 == 0 or idx == total:
                print(f"   ⏳ Progresso CA: {idx}/{total} consultados...")

    with transaction.atomic():
        for ca_obj, dados in resultados:
            ca_obj.ultima_consulta_api = agora
            if dados.get("encontrado"):
                if dados.get("data_validade"):
                    ca_obj.data_validade = dados["data_validade"]
                if dados.get("status"):
                    ca_obj.status = dados["status"]
                if dados.get("fabricante"):
                    ca_obj.fabricante = dados["fabricante"][:255]
                if dados.get("descricao_equipamento"):
                    ca_obj.descricao_equipamento = dados["descricao_equipamento"]

                ca_obj.save()
                atualizados += 1
                if ca_obj.status == "VENCIDO" or ca_obj.esta_vencido:
                    vencidos += 1
            else:
                # Marca que a consulta foi tentada para auditoria
                ca_obj.save(update_fields=["ultima_consulta_api"])
                nao_encontrados += 1

    print(
        f"✅ Consulta de CA concluída: {atualizados} atualizados "
        f"({vencidos} vencidos) | {nao_encontrados} não encontrados na base MTE."
    )
    return {
        "total": total,
        "atualizados": atualizados,
        "vencidos": vencidos,
        "nao_encontrados": nao_encontrados,
    }

