from datetime import datetime, date
from decimal import Decimal
import pandas as pd
from django.db import transaction
from integrations.hardness import HardnessAPI
from .models import NotaFiscal


def parse_decimal(valor):
    if valor is None or pd.isna(valor) or str(valor).strip() == "":
        return Decimal("0.00")
    if isinstance(valor, (int, float, Decimal)):
        return Decimal(str(valor))
    valor_limpo = str(valor).replace("R$", "").strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(valor_limpo)
    except Exception:
        return Decimal("0.00")


def parse_data(data_val):
    if not data_val or pd.isna(data_val):
        return None
    if isinstance(data_val, (date, datetime)):
        return data_val if isinstance(data_val, date) else data_val.date()
    
    data_str = str(data_val).strip()
    # Formato retornado pelo Hardness: 2026-09-22
    try:
        return datetime.strptime(data_str[:10], "%Y-%m-%d").date()
    except Exception:
        pass
    # Fallback caso venha em formato brasileiro: 22/09/2026
    try:
        return datetime.strptime(data_str[:10], "%d/%m/%Y").date()
    except Exception:
        return None


def sincronizar_notas_hardness(data_inicio="", data_fim="", empresa_nome="AMM EPIS"):
    api = HardnessAPI(verbose=True)
    api.login()
    
    empresa_id = api.empresas_dict.get(empresa_nome, {}).get("id_sistema", "1")
    api.trocar_empresa(empresa_id)
    
    api.filtrar(data_inicio=data_inicio, data_fim=data_fim, CFOP="VENDA", cancelada="N")
    df_notas = api.get_dados()

    if df_notas.empty:
        return 0, 0

    criadas = 0
    atualizadas = 0

    with transaction.atomic():
        for _, row in df_notas.iterrows():
            item = row.to_dict()

            # Mapeamento com base nas chaves do Hardness
            numero_nf = str(item.get("T007_Numero_Nota_Fiscal", "")).strip()
            
            # Se a nota não tiver número de NF, usa o ID interno do Hardness como fallback
            if not numero_nf or numero_nf == "nan":
                numero_nf = f"ID-{item.get('T007_Id', '')}"

            if not numero_nf or numero_nf == "ID-":
                continue

            cancelada = str(item.get("T007_Flag_Cancelada", "N")).strip()
            status = "CANCELADA" if cancelada == "S" else str(item.get("T005_Status", "FATURADA"))

            defaults = {
                "empresa": empresa_nome,
                "serie": str(item.get("T007_Flag_ACP", "")).strip(),
                "cliente_nome": str(item.get("D024_Nome_Empresa", item.get("D024_Nome_Fantasia", ""))).strip(),
                "cliente_documento": str(item.get("D024_Id", "")).strip(), # ID do cliente no ERP
                "data_emissao": parse_data(item.get("T007_Data_Emissao")),
                "valor_total": parse_decimal(item.get("T007_Valor_Total")),
                "cfop": str(item.get("D006_Codigo_CFOP", "")).strip(),
                "status": status,
                "dados_brutos": {k: (None if pd.isna(v) else v) for k, v in item.items()},
            }

            _, created = NotaFiscal.objects.update_or_create(
                numero_nota=numero_nf,
                defaults=defaults
            )

            if created:
                criadas += 1
            else:
                atualizadas += 1

    return criadas, atualizadas