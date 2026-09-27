from datetime import datetime, date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import time
import traceback
import unicodedata
import pandas as pd
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from integrations.piperun import PipeRunAPI
from integrations.hardness import HardnessAPI
from .models import NotaFiscal, MetaVendedor, Vendedor, LogSincronizacao


def parse_decimal(valor):
    if valor is None or pd.isna(valor) or str(valor).strip() == "":
        return Decimal("0.00")
    if isinstance(valor, (int, float, Decimal)):
        return Decimal(str(valor))

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
        return Decimal("0.00")


def parse_data(data_val):
    if not data_val or pd.isna(data_val):
        return None
    if isinstance(data_val, (date, datetime)):
        return data_val if isinstance(data_val, date) else data_val.date()

    data_str = str(data_val).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(data_str[:10], fmt).date()
        except Exception:
            continue
    return None


def registrar_execucao_sincronizacao(
    tipo: str,
    funcao_sync,
    origem: str = "MANUAL_PAINEL",
    usuario: str = "Sistema",
):
    """
    Executa uma função de sincronização registrando início, término, duração,
    contadores e eventuais erros na tabela LogSincronizacao.
    """
    log = LogSincronizacao.objects.create(
        tipo=tipo,
        origem=origem,
        status="EM_ANDAMENTO",
        usuario=usuario or "Sistema",
        mensagem="Sincronização iniciada...",
    )
    t0 = time.monotonic()
    try:
        resultado = funcao_sync()
        duracao = round(time.monotonic() - t0, 2)

        criados = 0
        atualizados = 0
        msg = "Sincronização concluída com sucesso."

        if isinstance(resultado, tuple) and len(resultado) == 2:
            criados, atualizados = int(resultado[0] or 0), int(resultado[1] or 0)
            msg = f"Concluído: {criados} criados, {atualizados} atualizados."
        elif isinstance(resultado, dict):
            criados = int(
                resultado.get("criados", 0)
                or (resultado.get("estoque_criados", 0) + resultado.get("itens_criados", 0))
            )
            atualizados = int(
                resultado.get("atualizados", 0)
                or (resultado.get("estoque_atualizados", 0) + resultado.get("itens_atualizados", 0))
            )
            msg = resultado.get("mensagem") or (
                f"Concluído: {criados} criados, {atualizados} atualizados."
            )

        log.status = "SUCESSO"
        log.finalizado_em = timezone.now()
        log.duracao_segundos = duracao
        log.registros_criados = criados
        log.registros_atualizados = atualizados
        log.mensagem = msg
        log.save()
        return log, resultado
    except Exception as exc:
        duracao = round(time.monotonic() - t0, 2)
        tb_curto = traceback.format_exc()[-1200:]
        log.status = "ERRO"
        log.finalizado_em = timezone.now()
        log.duracao_segundos = duracao
        log.mensagem = f"Erro: {exc}\n\n{tb_curto}"
        log.save()
        raise


def sincronizar_notas_hardness(data_inicio="", data_fim="", empresa_nome=None):
    """
    Sincroniza notas fiscais do Hardness ERP.
    Se empresa_nome for informado, sincroniza apenas ela.
    Se empresa_nome for None ou 'TODAS', itera sobre todas as empresas do empresas_dict.
    """
    api = HardnessAPI(verbose=True)
    api.login()

    if empresa_nome and empresa_nome.upper() != "TODAS":
        empresas_alvo = {empresa_nome: api.empresas_dict.get(empresa_nome, {"id_sistema": "1"})}
    else:
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
            if "vendedor.C007_Primeiro_Nome" in df_notas.columns:
                vendedores_lote = df_notas["vendedor.C007_Primeiro_Nome"].dropna().unique()
                cadastrar_vendedores_hardness(vendedores_lote)

        criadas_empresa = 0
        atualizadas_empresa = 0

        with transaction.atomic():
            for _, row in df_notas.iterrows():
                item = row.to_dict()

                numero_nf = str(item.get("T007_Numero_Nota_Fiscal", "")).strip()
                t007_id = str(item.get("T007_Id", "")).strip()

                if not numero_nf or numero_nf == "nan":
                    numero_nf = f"ID-{t007_id}"

                if not numero_nf or numero_nf == "ID-":
                    continue

                cancelada = str(item.get("T007_Flag_Cancelada", "N")).strip()
                status = "CANCELADA" if cancelada == "S" else str(item.get("T005_Status", "FATURADA"))

                defaults = {
                    "empresa": nome_empresa,
                    "serie": str(item.get("T007_Flag_ACP", "")).strip(),
                    "cliente_nome": str(
                        item.get("D024_Nome_Empresa", item.get("D024_Nome_Fantasia", ""))
                    ).strip(),
                    "cliente_documento": str(item.get("D024_Id", "")).strip(),
                    "data_emissao": parse_data(item.get("T007_Data_Emissao")),
                    "valor_total": parse_decimal(item.get("T007_Valor_Total_Produtos")),
                    "cfop": str(item.get("D006_Codigo_CFOP", "")).strip(),
                    "status": status,
                    "vendedor_nome": str(item.get("vendedor.C007_Primeiro_Nome", "DESCONHECIDO"))
                    .strip()
                    .upper(),
                    "dados_brutos": {k: (None if pd.isna(v) else v) for k, v in item.items()},
                }

                _, created = NotaFiscal.objects.update_or_create(
                    numero_nota=numero_nf,
                    defaults=defaults,
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
    Busca a data máxima gravada no banco (voltando 3 dias por segurança) e sincroniza
    todas as empresas até a data de hoje.
    """
    ultima_data = NotaFiscal.objects.aggregate(Max("data_emissao"))["data_emissao__max"]
    hoje = timezone.now().date()

    if ultima_data:
        data_inicio_dt = ultima_data - timedelta(days=3)
    else:
        data_inicio_dt = date(hoje.year, 1, 1)

    data_inicio_str = data_inicio_dt.strftime("%d/%m/%Y")
    data_fim_str = hoje.strftime("%d/%m/%Y")

    print(f"📅 Período identificado: {data_inicio_str} até {data_fim_str}")
    return sincronizar_notas_hardness(
        data_inicio=data_inicio_str,
        data_fim=data_fim_str,
        empresa_nome=empresa_nome,
    )


def carregar_dados_metas():
    """
    Tenta carregar o JSON existente em disco ou dispara a API do PipeRun.
    """
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

    api = PipeRunAPI()
    return api.export_goals_by_seller(salvar_json=True)


def sincronizar_metas_piperun():
    """
    Busca as metas via PipeRunAPI e grava diretamente no banco de dados (MetaVendedor).
    Suporta tanto o formato stats.data.processed[].byUser quanto metas diretas por user_id.
    """
    vincular_vendedores_piperun_automatico()

    api = PipeRunAPI()
    print("\n" + "=" * 50)
    print("🎯 Sincronizando Metas do PipeRun diretamente no Banco...")
    print("=" * 50)

    users_response = api.get_users()
    users_map = {}
    if isinstance(users_response, dict) and users_response.get("data"):
        for user in users_response["data"]:
            uid = user.get("id")
            if uid is not None:
                users_map[uid] = user.get("name", f"Usuário {uid}")
        print(f"✓ {len(users_map)} usuários identificados no PipeRun.")

    all_goals = api.get_goals(show=200)
    if isinstance(all_goals, dict) and all_goals.get("error"):
        raise RuntimeError(f"Erro na API PipeRun: {all_goals.get('error')}")

    goals_list = all_goals.get("data", []) if isinstance(all_goals, dict) else []

    if not goals_list:
        print("⚠️ Nenhuma meta retornada pelo PipeRun.")
        return 0, 0

    print(f"✓ {len(goals_list)} metas encontradas. Processando valores...")

    metas_por_periodo = {}

    for goal in goals_list:
        if not isinstance(goal, dict):
            continue
        goal_id = goal.get("id")
        goal_title = goal.get("title", "N/A")
        start_date = goal.get("start_at") or goal.get("start_date") or goal.get("created_at")

        if not start_date or not goal_id:
            continue

        try:
            dt_inicio = pd.to_datetime(start_date)
            mes = int(dt_inicio.month)
            ano = int(dt_inicio.year)
        except Exception:
            continue

        stats = api.get_goal_stats(goal_id)
        if not isinstance(stats, dict) or stats.get("error"):
            continue

        data_stats = stats.get("data") if isinstance(stats.get("data"), dict) else {}
        processed = data_stats.get("processed")

        extraiu_algum = False
        if isinstance(processed, list):
            for item in processed:
                if not isinstance(item, dict):
                    continue
                by_user = item.get("byUser") if isinstance(item.get("byUser"), dict) else item
                user_id = by_user.get("user_id")
                valor_raw = by_user.get("value") or by_user.get("target") or "0"

                if not user_id:
                    continue

                user_name = users_map.get(user_id, f"Usuário {user_id}")
                nome_vendedor = str(user_name).strip().upper()
                valor_dec = parse_decimal(valor_raw)
                if valor_dec <= 0:
                    continue

                extraiu_algum = True
                chave = (nome_vendedor, mes, ano)
                if chave not in metas_por_periodo or goal_id > metas_por_periodo[chave]["goal_id"]:
                    metas_por_periodo[chave] = {
                        "goal_id": goal_id,
                        "valor": valor_dec,
                        "titulo_meta": goal_title,
                    }

        # Fallback caso a meta na API venha atrelada diretamente ao user_id da goal
        if not extraiu_algum and goal.get("user_id"):
            uid = goal.get("user_id")
            val_fallback = api._extrair_valor_stats(stats)
            if val_fallback > 0:
                user_name = users_map.get(uid, f"Usuário {uid}")
                nome_vendedor = str(user_name).strip().upper()
                chave = (nome_vendedor, mes, ano)
                if chave not in metas_por_periodo or goal_id > metas_por_periodo[chave]["goal_id"]:
                    metas_por_periodo[chave] = {
                        "goal_id": goal_id,
                        "valor": Decimal(str(round(val_fallback, 2))),
                        "titulo_meta": goal_title,
                    }

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
                },
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
    nome_nfd = unicodedata.normalize("NFD", str(nome).strip().upper())
    sem_acentos = "".join(char for char in nome_nfd if unicodedata.category(char) != "Mn")
    partes = sem_acentos.split()
    return partes[0] if partes else ""


def cadastrar_vendedores_hardness(nomes_vendedores):
    """
    Garante que todos os nomes únicos vindos do Hardness existam na tabela Vendedor
    e possuam conta de usuário atrelada (com senha padrão 'amm@2026').
    """
    from .models import garantir_usuario_para_vendedor

    for nome in nomes_vendedores:
        nome_limpo = str(nome).strip().upper()
        if nome_limpo and nome_limpo not in ("NAN", "NONE", "DESCONHECIDO"):
            Vendedor.objects.get_or_create(nome_hardness=nome_limpo)
            garantir_usuario_para_vendedor(nome_limpo, senha_padrao="amm@2026")


def vincular_vendedores_piperun_automatico():
    """
    Para cada vendedor cujo nome_piperun está vazio, tenta atribuir
    automaticamente buscando os usuários cadastrados no PipeRun.
    """
    vendedores_sem_vinculo = Vendedor.objects.filter(
        Q(nome_piperun__isnull=True) | Q(nome_piperun="")
    )

    if not vendedores_sem_vinculo.exists():
        return

    api = PipeRunAPI()
    users_resp = api.get_users()
    if not isinstance(users_resp, dict) or "data" not in users_resp:
        return

    usuarios_piperun = [u.get("name", "") for u in users_resp["data"] if isinstance(u, dict) and u.get("name")]

    for vend in vendedores_sem_vinculo:
        primeiro_nome_hardness = normalizar_nome(vend.nome_hardness)
        if not primeiro_nome_hardness:
            continue

        for nome_pr in usuarios_piperun:
            if normalizar_nome(nome_pr) == primeiro_nome_hardness:
                vend.nome_piperun = str(nome_pr).strip().upper()
                vend.save()
                print(f"🔗 Vínculo automático criado: {vend.nome_hardness} ➔ {vend.nome_piperun}")
                break