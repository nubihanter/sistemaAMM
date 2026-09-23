from datetime import datetime, date
from decimal import Decimal
import pandas as pd
from django.db import transaction
from django.utils import timezone
from django.db.models import Max
from integrations.hardness import HardnessAPI
from .models import NotaFiscal


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
                    "valor_total": parse_decimal(item.get("T007_Valor_Total")),
                    "cfop": str(item.get("D006_Codigo_CFOP", "")).strip(),
                    "status": status,
                    "vendedor_nome": str(item.get("vendedor.C007_Primeiro_Nome", "")).strip().upper(),
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