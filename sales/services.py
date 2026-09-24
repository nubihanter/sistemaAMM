from datetime import datetime, date
from decimal import Decimal
import pandas as pd
from django.db import transaction
from django.utils import timezone
from django.db.models import Max
from integrations.piperun import PipeRunAPI
from integrations.hardness import HardnessAPI
from .models import NotaFiscal,MetaVendedor, Vendedor
import unicodedata
from pathlib import Path
import json
from django.db.models import Q

def parse_decimal(valor):
    if valor is None or pd.isna(valor) or str(valor).strip() == "":
        return Decimal("0.00")
    if isinstance(valor, (int, float, Decimal)):
        return Decimal(str(valor))
    
    val_str = str(valor).replace("R$", "").strip()

    # Trata casos em que existem ponto e vírgula juntos (ex: 1.500,50 ou 1,500.50)
    if "." in val_str and "," in val_str:
        if val_str.rfind(",") > val_str.rfind("."):
            # Formato brasileiro: 1.500,50 -> 1500.50
            val_str = val_str.replace(".", "").replace(",", ".")
        else:
            # Formato americano: 1,500.50 -> 1500.50
            val_str = val_str.replace(",", "")
    elif "," in val_str:
        # Apenas vírgula: 19,28 -> 19.28
        val_str = val_str.replace(",", ".")
    # Se tiver apenas ponto (ex: 19.28), mantém como está

    try:
        return Decimal(val_str)
    except Exception:
        return Decimal("0.00")


def parse_data(data_val):
    if not data_val or pd.isna(data_val):
        return None
    if isinstance(data_val, (date, datetime)):
        return data_val if isinstance(data_val, date) else data_val.date()
    
    data_str = str(data_val).strip()
    try:
        return datetime.strptime(data_str[:10], "%Y-%m-%d").date()
    except Exception:
        pass
    try:
        return datetime.strptime(data_str[:10], "%d/%m/%Y").date()
    except Exception:
        return None


def sincronizar_notas_hardness(data_inicio="", data_fim="", empresa_nome=None):
    """
    Sincroniza notas fiscais.
    Se empresa_nome for informado, sincroniza apenas ela.
    Se empresa_nome for None ou 'TODAS', itera sobre todas as empresas do empresas_dict.
    """
    api = HardnessAPI(verbose=True)
    api.login()

    # Define quais empresas serão sincronizadas
    if empresa_nome and empresa_nome.upper() != "TODAS":
        empresas_alvo = {empresa_nome: api.empresas_dict.get(empresa_nome, {"id_sistema": "1"})}
    else:
        # Pega todas as empresas configuradas no dicionário
        empresas_alvo = api.empresas_dict

    total_criadas = 0
    total_atualizadas = 0

    for nome_empresa, config_empresa in empresas_alvo.items():
        empresa_id = config_empresa.get("id_sistema")
        print(f"\n==================================================")
        print(f"🏢 Sincronizando: {nome_empresa} (ID: {empresa_id})")
        print(f"==================================================")

        sucesso_troca = api.trocar_empresa(empresa_id)
        if not sucesso_troca:
            print(f"⚠️ Pulando {nome_empresa} devido a erro na troca de sessão.")
            continue

        filtro_ok = api.filtrar(data_inicio=data_inicio, data_fim=data_fim, CFOP="VENDA", cancelada="N")
        if not filtro_ok:
            print(f"⚠️ Não foi possível aplicar o filtro em {nome_empresa}.")
            continue

        df_notas = api.get_dados()
        if df_notas.empty:
            print(f"ℹ️ Nenhuma nota encontrada para {nome_empresa} no período.")
            continue
        else:
            vendedores_lote = df_notas["vendedor.C007_Primeiro_Nome"].dropna().unique()
            cadastrar_vendedores_hardness(vendedores_lote)

        criadas_empresa = 0
        atualizadas_empresa = 0

        with transaction.atomic():
            for _, row in df_notas.iterrows():
                item = row.to_dict()

                numero_nf = str(item.get("T007_Numero_Nota_Fiscal", "")).strip()
                t007_id = str(item.get("T007_Id", "")).strip()

                # Fallback se não houver número de nota formal
                if not numero_nf or numero_nf == "nan":
                    numero_nf = f"ID-{t007_id}"

                if not numero_nf or numero_nf == "ID-":
                    continue

                cancelada = str(item.get("T007_Flag_Cancelada", "N")).strip()
                status = "CANCELADA" if cancelada == "S" else str(item.get("T005_Status", "FATURADA"))

                defaults = {
                    "empresa": nome_empresa,
                    "serie": str(item.get("T007_Flag_ACP", "")).strip(),
                    "cliente_nome": str(item.get("D024_Nome_Empresa", item.get("D024_Nome_Fantasia", ""))).strip(),
                    "cliente_documento": str(item.get("D024_Id", "")).strip(),
                    "data_emissao": parse_data(item.get("T007_Data_Emissao")),
                    "valor_total": parse_decimal(item.get("T007_Valor_Total_Produtos")),
                    "cfop": str(item.get("D006_Codigo_CFOP", "")).strip(),
                    "status": status,
                    "vendedor_nome": str(item.get("vendedor.C007_Primeiro_Nome", "DESCONHECIDO")).strip().upper(),
                    "dados_brutos": {k: (None if pd.isna(v) else v) for k, v in item.items()},
                }

                # Para evitar conflitos de numeração entre empresas distintas, 
                # o identificador único no banco considera o ID único do ERP ou (empresa, numero_nota)
                chave_busca = f"{empresa_id}-{numero_nf}" if "numero_nota" in defaults else numero_nf

                _, created = NotaFiscal.objects.update_or_create(
                    numero_nota=numero_nf,
                    defaults=defaults
                )

                if created:
                    criadas_empresa += 1
                else:
                    atualizadas_empresa += 1

        print(f"✅ {nome_empresa}: {criadas_empresa} notas criadas, {atualizadas_empresa} atualizadas.")
        total_criadas += criadas_empresa
        total_atualizadas += atualizadas_empresa

    return total_criadas, total_atualizadas


def sincronizar_desde_ultimo_registro(empresa_nome=None):
    """
    Busca a data máxima gravada no banco e sincroniza todas as empresas até a data de hoje.
    """
    ultima_data = NotaFiscal.objects.aggregate(Max('data_emissao'))['data_emissao__max']
    hoje = timezone.now().date()

    if ultima_data:
        data_inicio_dt = ultima_data
    else:
        data_inicio_dt = date(hoje.year, 1, 1)

    data_inicio_str = data_inicio_dt.strftime("%d/%m/%Y")
    data_fim_str = hoje.strftime("%d/%m/%Y")

    print(f"📅 Período identificado: {data_inicio_str} até {data_fim_str}")
    return sincronizar_notas_hardness(
        data_inicio=data_inicio_str,
        data_fim=data_fim_str,
        empresa_nome=empresa_nome
    )

def carregar_dados_metas():
    """
    Tenta carregar o JSON existente em disco ou dispara a API do PipeRun.
    """
    # 1. Tenta carregar do arquivo local se ele já existir
    locais_possiveis = [
        Path(__file__).resolve().parent.parent / "data" / "metas_por_vendedores.json",
        Path(__file__).resolve().parent.parent / "integrations" / "data" / "metas_por_vendedores.json",
    ]
    
    for caminho in locais_possiveis:
        if caminho.exists():
            try:
                with open(caminho, "r", encoding="utf-8") as f:
                    dados = json.load(f)
                    if dados and isinstance(dados, list):
                        return dados
            except Exception:
                pass

    # 2. Se não encontrou o arquivo local, busca via PipeRunAPI
    api = PipeRunAPI()
    return api.export_goals_by_seller(salvar_json=True)


def sincronizar_metas_piperun():
    """
    Busca as metas via PipeRunAPI usando a lógica validada do getGoalsPipeRun
    e grava diretamente no banco de dados (MetaVendedor), sem gerar ou ler JSON.
    """
    vincular_vendedores_piperun_automatico()

    api = PipeRunAPI()
    print("\n" + "="*50)
    print("🎯 Sincronizando Metas do PipeRun diretamente no Banco...")
    print("="*50)

    # 1. Mapear ID -> Nome de Usuário
    users_response = api.get_users()
    users_map = {}
    if users_response and users_response.get("data"):
        for user in users_response["data"]:
            users_map[user["id"]] = user.get("name", f"Usuário {user['id']}")
        print(f"✓ {len(users_map)} usuários identificados no PipeRun.")

    # 2. Buscar lista de metas
    all_goals = api.get_goals(show=100)
    goals_list = all_goals.get("data", []) if all_goals else []

    if not goals_list:
        print("⚠️ Nenhuma meta retornada pelo PipeRun.")
        return 0, 0

    print(f"✓ {len(goals_list)} metas encontradas. Processando valores...")

    # Dicionário para reter a meta de maior goal_id por (vendedor, mes, ano)
    metas_por_periodo = {}

    # 3. Processar cada meta e extrair byUser via endpoint /stats
    for goal in goals_list:
        goal_id = goal.get("id")
        goal_title = goal.get("title", "N/A")
        start_date = goal.get("start_at") or goal.get("start_date") or goal.get("created_at")

        if not start_date:
            continue

        try:
            dt_inicio = pd.to_datetime(start_date)
            mes = dt_inicio.month
            ano = dt_inicio.year
        except Exception:
            continue

        stats = api.get_goal_stats(goal_id)
        if not stats or not stats.get("data") or not stats["data"].get("processed"):
            continue

        processed = stats["data"]["processed"]
        for item in processed:
            by_user = item.get("byUser", {})
            user_id = by_user.get("user_id")
            valor_raw = by_user.get("value", "0")

            if not user_id:
                continue

            user_name = users_map.get(user_id, f"Usuário {user_id}")
            nome_vendedor = str(user_name).strip().upper()

            try:
                valor_dec = Decimal(str(valor_raw))
            except Exception:
                valor_dec = Decimal("0.00")

            chave = (nome_vendedor, mes, ano)

            # Se houver mais de uma meta para o mesmo período, mantém a de maior ID (mais recente)
            if chave not in metas_por_periodo or goal_id > metas_por_periodo[chave]["goal_id"]:
                metas_por_periodo[chave] = {
                    "goal_id": goal_id,
                    "valor": valor_dec,
                    "titulo_meta": goal_title
                }

    # 4. Gravar diretamente na tabela MetaVendedor
    criadas = 0
    atualizadas = 0

    with transaction.atomic():
        for (nome_vendedor, mes, ano), info in metas_por_periodo.items():
            _, created = MetaVendedor.objects.update_or_create(
                vendedor_nome=nome_vendedor,
                mes=mes,
                ano=ano,
                defaults={
                    "valor": info["valor"],
                    "titulo_meta": info["titulo_meta"],
                }
            )
            if created:
                criadas += 1
            else:
                atualizadas += 1

    print(f"✅ Metas gravadas no banco: {criadas} criadas | {atualizadas} atualizadas.\n")
    return criadas, atualizadas

def normalizar_nome(nome):
    """Extrai apenas o primeiro nome em maiúsculas sem acentos."""
    if not nome:
        return ""
    nome_nfd = unicodedata.normalize('NFD', str(nome).strip().upper())
    sem_acentos = ''.join(char for char in nome_nfd if unicodedata.category(char) != 'Mn')
    partes = sem_acentos.split()
    return partes[0] if partes else ""


def cadastrar_vendedores_hardness(nomes_vendedores):
    """
    Garante que todos os nomes únicos vindos do Hardness existam na tabela Vendedor.
    """
    for nome in nomes_vendedores:
        nome_limpo = str(nome).strip().upper()
        if nome_limpo and nome_limpo != "NAN":
            Vendedor.objects.get_or_create(nome_hardness=nome_limpo)


def vincular_vendedores_piperun_automatico():
    """
    Para cada vendedor cujo nome_piperun está vazio, tenta atribuir
    automaticamente buscando os usuários cadastrados no PipeRun.
    Se já tiver registro no field, pula esse passo.
    """
    vendedores_sem_vinculo = Vendedor.objects.filter(
        Q(nome_piperun__isnull=True) | Q(nome_piperun="")
    )

    if not vendedores_sem_vinculo.exists():
        return

    # Busca a lista de usuários no PipeRun usando a lib validada
    api = PipeRunAPI()
    users_resp = api.get_users()
    if not isinstance(users_resp, dict) or "data" not in users_resp:
        return

    usuarios_piperun = [u.get("name", "") for u in users_resp["data"] if u.get("name")]

    for vend in vendedores_sem_vinculo:
        primeiro_nome_hardness = normalizar_nome(vend.nome_hardness)
        if not primeiro_nome_hardness:
            continue

        # Procura um match no PipeRun pelo primeiro nome normalizado
        for nome_pr in usuarios_piperun:
            if normalizar_nome(nome_pr) == primeiro_nome_hardness:
                vend.nome_piperun = str(nome_pr).strip().upper()
                vend.save()
                print(f"🔗 Vínculo automático criado: {vend.nome_hardness} ➔ {vend.nome_piperun}")
                break