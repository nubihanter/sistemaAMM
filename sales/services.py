import builtins as _builtins
from datetime import datetime, date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
import traceback
import unicodedata
import pandas as pd
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from integrations.piperun import PipeRunAPI
from integrations.hardness import HardnessAPI
from .models import (
    ContaPagar,
    ContaReceber,
    LogSincronizacao,
    MetaVendedor,
    NotaFiscal,
    Orcamento,
    Vendedor,
)


def print(*args, **kwargs):
    """Wrapper seguro de print para evitar UnicodeEncodeError em consoles Windows (cp1252)."""
    try:
        _builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        texto = " ".join(str(a) for a in args)
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        texto_seguro = texto.encode(enc, errors="replace").decode(enc, errors="replace")
        _builtins.print(texto_seguro, **kwargs)


def _limpar_str(valor, max_len: int = None):
    """Converte valor do DataFrame/JSON para string limpa ou None se vazio/NaN."""
    if valor is None or pd.isna(valor):
        return None
    s = str(valor).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    if max_len:
        return s[:max_len]
    return s


def parse_int(valor, default: int = 0) -> int:
    if valor is None or pd.isna(valor):
        return default
    try:
        return int(float(str(valor).strip()))
    except Exception:
        return default


def parse_decimal(valor, casas: int = 2, max_digits: int = 12):
    zero = Decimal("10") ** (-casas) * Decimal("0")
    if valor is None or pd.isna(valor) or str(valor).strip() == "":
        return zero

    q = Decimal("10") ** (-casas)
    limite = (Decimal("10") ** (max_digits - casas)) - q

    if isinstance(valor, (int, float, Decimal)):
        try:
            dec = Decimal(str(valor)).quantize(q)
            if dec > limite:
                return limite
            if dec < -limite:
                return -limite
            return dec
        except Exception:
            return zero

    val_str = str(valor).replace("R$", "").strip()

    if "." in val_str and "," in val_str:
        if val_str.rfind(",") > val_str.rfind("."):
            val_str = val_str.replace(".", "").replace(",", ".")
        else:
            val_str = val_str.replace(",", "")
    elif "," in val_str:
        val_str = val_str.replace(",", ".")

    try:
        dec = Decimal(val_str).quantize(q)
        if dec > limite:
            return limite
        if dec < -limite:
            return -limite
        return dec
    except Exception:
        return zero


def parse_data(data_val):
    if not data_val or pd.isna(data_val):
        return None
    if isinstance(data_val, (date, datetime)):
        return data_val if isinstance(data_val, date) else data_val.date()

    data_str = str(data_val).strip()
    if not data_str or data_str.startswith("0000-00-00") or data_str.lower() in ("nan", "none", "null"):
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(data_str[:10], fmt).date()
        except Exception:
            continue
    return None


def _extrair_contadores_resultado(resultado):
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
            or (resultado.get("cr_criados", 0) + resultado.get("cp_criados", 0))
        )
        atualizados = int(
            resultado.get("atualizados", 0)
            or (resultado.get("estoque_atualizados", 0) + resultado.get("itens_atualizados", 0))
            or (resultado.get("cr_atualizados", 0) + resultado.get("cp_atualizados", 0))
        )
        msg = resultado.get("mensagem") or (
            f"Concluído: {criados} criados, {atualizados} atualizados."
        )
    return criados, atualizados, msg


def registrar_execucao_sincronizacao(
    tipo: str,
    funcao_sync,
    origem: str = "MANUAL_PAINEL",
    usuario: str = "Sistema",
):
    """
    Executa uma função de sincronização de forma síncrona registrando início, término,
    duração, contadores e eventuais erros na tabela LogSincronizacao.
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
        criados, atualizados, msg = _extrair_contadores_resultado(resultado)

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


def disparar_sincronizacao_background(
    tipo: str,
    funcao_sync,
    origem: str = "MANUAL_PAINEL",
    usuario: str = "Sistema",
):
    """
    Dispara uma tarefa de sincronização em thread de segundo plano (não bloqueia a requisição HTTP)
    e previne execuções concorrentes duplicadas do mesmo tipo nos últimos 15 minutos.
    Retorna (iniciou_novo: bool, log: LogSincronizacao).
    """
    import threading
    from django.db import close_old_connections

    janela_ativa = timezone.now() - timedelta(minutes=15)
    em_andamento = (
        LogSincronizacao.objects.filter(
            status="EM_ANDAMENTO",
            iniciado_em__gte=janela_ativa,
        )
        .filter(Q(tipo=tipo) | Q(tipo="COMPLETA"))
        .order_by("-iniciado_em")
        .first()
    )
    if em_andamento:
        return False, em_andamento

    log = LogSincronizacao.objects.create(
        tipo=tipo,
        origem=origem,
        status="EM_ANDAMENTO",
        usuario=usuario or "Sistema",
        mensagem="Sincronização em execução em segundo plano...",
    )

    def _worker():
        close_old_connections()
        t0 = time.monotonic()
        try:
            resultado = funcao_sync()
            duracao = round(time.monotonic() - t0, 2)
            criados, atualizados, msg = _extrair_contadores_resultado(resultado)
            log.status = "SUCESSO"
            log.finalizado_em = timezone.now()
            log.duracao_segundos = duracao
            log.registros_criados = criados
            log.registros_atualizados = atualizados
            log.mensagem = msg
            log.save()
        except Exception as exc:
            duracao = round(time.monotonic() - t0, 2)
            tb_curto = traceback.format_exc()[-1200:]
            log.status = "ERRO"
            log.finalizado_em = timezone.now()
            log.duracao_segundos = duracao
            log.mensagem = f"Erro: {exc}\n\n{tb_curto}"
            log.save()
        finally:
            close_old_connections()

    thread = threading.Thread(
        target=_worker,
        name=f"sync-{tipo.lower()}-{log.id}",
        daemon=True,
    )
    thread.start()
    return True, log


def sincronizar_notas_hardness(data_inicio="", data_fim="", empresa_nome=None):
    """
    Sincroniza notas fiscais do Hardness ERP em lotes otimizados (bulk_create / bulk_update).
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

        # Prepara registros deduplicados por numero_nota dentro da empresa
        registros_preparados = {}
        for item in df_notas.to_dict("records"):
            numero_nf = str(item.get("T007_Numero_Nota_Fiscal", "")).strip()
            t007_id = str(item.get("T007_Id", "")).strip()

            if not numero_nf or numero_nf == "nan":
                numero_nf = f"ID-{t007_id}"

            if not numero_nf or numero_nf == "ID-":
                continue

            cancelada = str(item.get("T007_Flag_Cancelada", "N")).strip()
            status = "CANCELADA" if cancelada == "S" else str(item.get("T005_Status", "FATURADA"))

            registros_preparados[numero_nf] = {
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

        existentes_map = {
            nf.numero_nota: nf
            for nf in NotaFiscal.objects.filter(
                empresa=nome_empresa,
                numero_nota__in=list(registros_preparados.keys()),
            )
        }

        agora = timezone.now()
        para_criar = []
        para_atualizar = []
        campos_update = [
            "serie",
            "cliente_nome",
            "cliente_documento",
            "data_emissao",
            "valor_total",
            "cfop",
            "status",
            "vendedor_nome",
            "dados_brutos",
            "data_sincronizacao",
        ]

        for numero_nf, defaults in registros_preparados.items():
            nf_existente = existentes_map.get(numero_nf)
            if nf_existente:
                for campo, valor in defaults.items():
                    setattr(nf_existente, campo, valor)
                nf_existente.data_sincronizacao = agora
                para_atualizar.append(nf_existente)
            else:
                para_criar.append(NotaFiscal(numero_nota=numero_nf, **defaults))

        with transaction.atomic():
            if para_criar:
                NotaFiscal.objects.bulk_create(para_criar, batch_size=500)
            if para_atualizar:
                NotaFiscal.objects.bulk_update(para_atualizar, fields=campos_update, batch_size=500)

        criadas_empresa = len(para_criar)
        atualizadas_empresa = len(para_atualizar)

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


def sincronizar_metas_piperun(forcar_api: bool = True):
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


def sincronizar_contas_receber_hardness(
    data_inicio: str,
    data_fim: str,
    empresa_nome: str = None,
    campo_data: str = "emissao",
    api: HardnessAPI = None,
):
    """
    Sincroniza os títulos de Contas a Receber (fin001grid01) do Hardness ERP em lote
    (bulk_create / bulk_update) para as empresas do grupo.
    """
    api = api or HardnessAPI()
    if not api.autenticado:
        api.login()

    hoje = timezone.now().date()
    total_criados = 0
    total_atualizados = 0

    empresas_para_rodar = api.empresas_dict.items()
    if empresa_nome and empresa_nome in api.empresas_dict:
        empresas_para_rodar = [(empresa_nome, api.empresas_dict[empresa_nome])]

    for nome_empresa, dados_empresa in empresas_para_rodar:
        empresa_id = dados_empresa["id_sistema"]
        print(f"\n💰 [Contas a Receber] Sincronizando: {nome_empresa} ({data_inicio} a {data_fim})")

        if not api.trocar_empresa(empresa_id):
            print(f"⚠️ Pulando {nome_empresa} (Contas a Receber) por falha na troca de sessão.")
            continue

        if not api.filtrar_contas_receber(data_inicio=data_inicio, data_fim=data_fim, campo_data=campo_data):
            print(f"⚠️ Não foi possível aplicar filtro de Contas a Receber em {nome_empresa}.")
            continue

        df_cr = api.get_dados(url=api.contas_receber_url)
        if df_cr.empty:
            print(f"ℹ️ Nenhum título a receber encontrado para {nome_empresa} no período.")
            continue

        registros_preparados = {}
        for item in df_cr.to_dict("records"):
            id_titulo = _limpar_str(item.get("T002_Id"))
            if not id_titulo:
                continue

            dt_emissao = parse_data(item.get("T002_Data_Emissao"))
            dt_vencimento = parse_data(
                item.get("T002_Data_Vencimento") or item.get("T117_Data_Vencimento")
            )
            dt_recebimento = parse_data(item.get("T002_Data_Recebimento"))
            dt_baixa = parse_data(item.get("T002_Data_Baixa"))

            val_duplicata = parse_decimal(item.get("T002_Valor_Duplicata"))
            val_juros = parse_decimal(item.get("T002_Valor_Juros"))
            val_desconto = parse_decimal(item.get("T002_Valor_Desconto"))
            val_total = parse_decimal(item.get("T002_Valor_Total"))
            val_recebido = parse_decimal(item.get("T002_Valor_Recebido"))
            saldo_raw = (
                item.get("T002_Valor_Saldo")
                if item.get("T002_Valor_Saldo") is not None
                else item.get("aReceber")
            )
            val_saldo = parse_decimal(saldo_raw)
            val_comissao = parse_decimal(item.get("T002_Valor_Comissao"))

            cancelada = str(item.get("T002_Flag_Cancelada", "N")).strip().upper() == "S"
            flag_status = str(item.get("T002_Flag_Status", "")).strip()

            if cancelada:
                status_calc = "CANCELADO"
            elif (
                dt_recebimento is not None
                or (val_saldo <= Decimal("0.00") and (val_recebido > Decimal("0.00") or flag_status in ("2", "3")))
            ):
                status_calc = "RECEBIDO"
            elif dt_vencimento and dt_vencimento < hoje:
                status_calc = "VENCIDO"
            else:
                status_calc = "A_VENCER"

            cliente_nome = _limpar_str(
                item.get("D024_Nome_Fantasia") or item.get("substr(D024_Nome_Empresa,1,40)"),
                max_len=255,
            )
            vend_raw = _limpar_str(item.get("C007_Nome"), max_len=100)

            registros_preparados[id_titulo] = {
                "empresa": nome_empresa,
                "numero_documento": _limpar_str(item.get("T002_Numero_Documento"), max_len=60),
                "numero_duplicata": _limpar_str(item.get("T002_Numero_Duplicata"), max_len=60),
                "parcela": _limpar_str(item.get("Parcelas"), max_len=30),
                "cliente_id_erp": _limpar_str(item.get("T002_D024_Id") or item.get("D024_Id"), max_len=30),
                "cliente_nome": cliente_nome,
                "cliente_documento": _limpar_str(item.get("D024_Cnpj"), max_len=30),
                "vendedor_nome": vend_raw.upper() if vend_raw else None,
                "data_emissao": dt_emissao,
                "data_vencimento": dt_vencimento,
                "data_recebimento": dt_recebimento,
                "data_baixa": dt_baixa,
                "prazo_dias": parse_int(item.get("Prazo")),
                "dias_atraso": parse_int(item.get("Atraso")),
                "valor_duplicata": val_duplicata,
                "valor_juros": val_juros,
                "valor_desconto": val_desconto,
                "valor_total": val_total,
                "valor_recebido": val_recebido,
                "valor_saldo": val_saldo,
                "valor_comissao": val_comissao,
                "portador": _limpar_str(item.get("D027_Portador"), max_len=100),
                "subconta": _limpar_str(item.get("D014_SubConta"), max_len=150),
                "grupo_conta": _limpar_str(item.get("D032_Descricao"), max_len=150),
                "nosso_numero": _limpar_str(item.get("T117_Nosso_Numero"), max_len=60),
                "observacao": _limpar_str(item.get("T002_Observacao")),
                "status": status_calc,
                "cancelada": cancelada,
                "dados_brutos": {k: (None if pd.isna(v) else v) for k, v in item.items()},
            }

        existentes_map = {
            obj.id_titulo_erp: obj
            for obj in ContaReceber.objects.filter(
                empresa=nome_empresa,
                id_titulo_erp__in=list(registros_preparados.keys()),
            )
        }

        agora = timezone.now()
        para_criar = []
        para_atualizar = []
        campos_update = [
            "numero_documento",
            "numero_duplicata",
            "parcela",
            "cliente_id_erp",
            "cliente_nome",
            "cliente_documento",
            "vendedor_nome",
            "data_emissao",
            "data_vencimento",
            "data_recebimento",
            "data_baixa",
            "prazo_dias",
            "dias_atraso",
            "valor_duplicata",
            "valor_juros",
            "valor_desconto",
            "valor_total",
            "valor_recebido",
            "valor_saldo",
            "valor_comissao",
            "portador",
            "subconta",
            "grupo_conta",
            "nosso_numero",
            "observacao",
            "status",
            "cancelada",
            "dados_brutos",
            "data_sincronizacao",
        ]

        for id_titulo, defaults in registros_preparados.items():
            existente = existentes_map.get(id_titulo)
            if existente:
                for campo, valor in defaults.items():
                    setattr(existente, campo, valor)
                existente.data_sincronizacao = agora
                para_atualizar.append(existente)
            else:
                para_criar.append(ContaReceber(id_titulo_erp=id_titulo, **defaults))

        with transaction.atomic():
            if para_criar:
                ContaReceber.objects.bulk_create(para_criar, batch_size=500)
            if para_atualizar:
                ContaReceber.objects.bulk_update(para_atualizar, fields=campos_update, batch_size=500)

        c_emp = len(para_criar)
        a_emp = len(para_atualizar)
        print(f"✅ [Contas a Receber] {nome_empresa}: {c_emp} criados, {a_emp} atualizados.")
        total_criados += c_emp
        total_atualizados += a_emp

    return total_criados, total_atualizados


def sincronizar_contas_pagar_hardness(
    data_inicio: str,
    data_fim: str,
    empresa_nome: str = None,
    campo_data: str = "emissao",
    api: HardnessAPI = None,
):
    """
    Sincroniza os títulos de Contas a Pagar (fin002grid01) do Hardness ERP em lote
    (bulk_create / bulk_update) para as empresas do grupo.
    """
    api = api or HardnessAPI()
    if not api.autenticado:
        api.login()

    hoje = timezone.now().date()
    total_criados = 0
    total_atualizados = 0

    empresas_para_rodar = api.empresas_dict.items()
    if empresa_nome and empresa_nome in api.empresas_dict:
        empresas_para_rodar = [(empresa_nome, api.empresas_dict[empresa_nome])]

    for nome_empresa, dados_empresa in empresas_para_rodar:
        empresa_id = dados_empresa["id_sistema"]
        print(f"\n💸 [Contas a Pagar] Sincronizando: {nome_empresa} ({data_inicio} a {data_fim})")

        if not api.trocar_empresa(empresa_id):
            print(f"⚠️ Pulando {nome_empresa} (Contas a Pagar) por falha na troca de sessão.")
            continue

        if not api.filtrar_contas_pagar(data_inicio=data_inicio, data_fim=data_fim, campo_data=campo_data):
            print(f"⚠️ Não foi possível aplicar filtro de Contas a Pagar em {nome_empresa}.")
            continue

        df_cp = api.get_dados(url=api.contas_pagar_url)
        if df_cp.empty:
            print(f"ℹ️ Nenhum título a pagar encontrado para {nome_empresa} no período.")
            continue

        registros_preparados = {}
        chave_grupo_expr = 'IF(D154_Id>0,CONCAT(D154_Descricao," - ",D014_SubConta),D014_SubConta)'

        for item in df_cp.to_dict("records"):
            id_titulo = _limpar_str(item.get("T015_Id"))
            if not id_titulo:
                continue

            dt_emissao = parse_data(item.get("T015_Data_Emissao"))
            dt_vencimento = parse_data(item.get("T015_Data_Vencimento"))
            dt_pagamento = parse_data(item.get("T015_Data_Pagamento"))

            val_duplicata = parse_decimal(item.get("T015_Valor_Duplicata"))
            val_juros = parse_decimal(item.get("T015_Valor_Juros"))
            val_desconto = parse_decimal(item.get("T015_Valor_Desconto"))
            val_total = parse_decimal(item.get("T015_Valor_Total"))
            val_pago = parse_decimal(item.get("T015_Valor_Pago"))
            val_saldo = parse_decimal(item.get("T015_Valor_Saldo"))

            cancelada = str(item.get("T015_Flag_Cancelada", "N")).strip().upper() == "S"

            if cancelada:
                status_calc = "CANCELADO"
            elif dt_pagamento is not None or (val_saldo <= Decimal("0.00") and val_pago > Decimal("0.00")):
                status_calc = "PAGO"
            elif dt_vencimento and dt_vencimento < hoje:
                status_calc = "VENCIDO"
            else:
                status_calc = "A_VENCER"

            registros_preparados[id_titulo] = {
                "empresa": nome_empresa,
                "numero_documento": _limpar_str(item.get("T015_Numero_Documento"), max_len=60),
                "numero_duplicata": _limpar_str(item.get("T015_Numero_Duplicata"), max_len=60),
                "parcela": _limpar_str(item.get("Parcelas"), max_len=30),
                "fornecedor_id_erp": _limpar_str(item.get("T015_D024_Id"), max_len=30),
                "fornecedor_nome": _limpar_str(item.get("D024_Nome_Empresa"), max_len=255),
                "fornecedor_documento": _limpar_str(item.get("concat(D024_Cnpj,D024_Cpf)"), max_len=30),
                "data_emissao": dt_emissao,
                "data_vencimento": dt_vencimento,
                "data_pagamento": dt_pagamento,
                "prazo_dias": parse_int(item.get("Prazo")),
                "dias_atraso": parse_int(item.get("Atraso")),
                "valor_duplicata": val_duplicata,
                "valor_juros": val_juros,
                "valor_desconto": val_desconto,
                "valor_total": val_total,
                "valor_pago": val_pago,
                "valor_saldo": val_saldo,
                "centro_custo": _limpar_str(item.get("D073_Nome"), max_len=150),
                "subconta": _limpar_str(item.get("D014_SubConta"), max_len=150),
                "grupo_conta": _limpar_str(
                    item.get(chave_grupo_expr) or item.get("D014_Grupo"), max_len=150
                ),
                "portador": _limpar_str(item.get("D031_Portador"), max_len=100),
                "codigo_barras": _limpar_str(item.get("T015_Codigo_Barras"), max_len=120),
                "observacao": _limpar_str(item.get("T015_Observacao")),
                "status": status_calc,
                "cancelada": cancelada,
                "dados_brutos": {k: (None if pd.isna(v) else v) for k, v in item.items()},
            }

        existentes_map = {
            obj.id_titulo_erp: obj
            for obj in ContaPagar.objects.filter(
                empresa=nome_empresa,
                id_titulo_erp__in=list(registros_preparados.keys()),
            )
        }

        agora = timezone.now()
        para_criar = []
        para_atualizar = []
        campos_update = [
            "numero_documento",
            "numero_duplicata",
            "parcela",
            "fornecedor_id_erp",
            "fornecedor_nome",
            "fornecedor_documento",
            "data_emissao",
            "data_vencimento",
            "data_pagamento",
            "prazo_dias",
            "dias_atraso",
            "valor_duplicata",
            "valor_juros",
            "valor_desconto",
            "valor_total",
            "valor_pago",
            "valor_saldo",
            "centro_custo",
            "subconta",
            "grupo_conta",
            "portador",
            "codigo_barras",
            "observacao",
            "status",
            "cancelada",
            "dados_brutos",
            "data_sincronizacao",
        ]

        for id_titulo, defaults in registros_preparados.items():
            existente = existentes_map.get(id_titulo)
            if existente:
                for campo, valor in defaults.items():
                    setattr(existente, campo, valor)
                existente.data_sincronizacao = agora
                para_atualizar.append(existente)
            else:
                para_criar.append(ContaPagar(id_titulo_erp=id_titulo, **defaults))

        with transaction.atomic():
            if para_criar:
                ContaPagar.objects.bulk_create(para_criar, batch_size=500)
            if para_atualizar:
                ContaPagar.objects.bulk_update(para_atualizar, fields=campos_update, batch_size=500)

        c_emp = len(para_criar)
        a_emp = len(para_atualizar)
        print(f"✅ [Contas a Pagar] {nome_empresa}: {c_emp} criados, {a_emp} atualizados.")
        total_criados += c_emp
        total_atualizados += a_emp

    return total_criados, total_atualizados


def sincronizar_financeiro_hardness(
    data_inicio: str = None,
    data_fim: str = None,
    empresa_nome: str = None,
    dias_retroativos_padrao: int = 30,
):
    """
    Rotina unificada de atualização financeira (Contas a Receber + Contas a Pagar).
    Se data_inicio não for informada:
      - Caso o banco esteja vazio, busca desde 01/01 do ano atual.
      - Caso já possua registros, volta `dias_retroativos_padrao` (padrão 30 dias) para
        capturar baixas/pagamentos de títulos emitidos anteriormente.
    """
    hoje = timezone.now().date()
    data_fim_str = data_fim or hoje.strftime("%d/%m/%Y")

    if data_inicio:
        data_inicio_cr = data_inicio
        data_inicio_cp = data_inicio
    else:
        ult_cr = ContaReceber.objects.aggregate(Max("data_emissao"))["data_emissao__max"]
        ult_cp = ContaPagar.objects.aggregate(Max("data_emissao"))["data_emissao__max"]
        inicio_ano = date(hoje.year, 1, 1)

        dt_cr = (ult_cr - timedelta(days=dias_retroativos_padrao)) if ult_cr else inicio_ano
        dt_cp = (ult_cp - timedelta(days=dias_retroativos_padrao)) if ult_cp else inicio_ano
        data_inicio_cr = dt_cr.strftime("%d/%m/%Y")
        data_inicio_cp = dt_cp.strftime("%d/%m/%Y")

    api = HardnessAPI()
    api.login()

    cr_c, cr_a = sincronizar_contas_receber_hardness(
        data_inicio=data_inicio_cr,
        data_fim=data_fim_str,
        empresa_nome=empresa_nome,
        api=api,
    )
    cp_c, cp_a = sincronizar_contas_pagar_hardness(
        data_inicio=data_inicio_cp,
        data_fim=data_fim_str,
        empresa_nome=empresa_nome,
        api=api,
    )

    return {
        "cr_criados": cr_c,
        "cr_atualizados": cr_a,
        "cp_criados": cp_c,
        "cp_atualizados": cp_a,
        "criados": cr_c + cp_c,
        "atualizados": cr_a + cp_a,
        "mensagem": f"Contas a Receber: +{cr_c}/{cr_a} | Contas a Pagar: +{cp_c}/{cp_a}",
    }


def sincronizar_orcamentos_hardness(
    data_inicio: str,
    data_fim: str,
    empresa_nome: str = None,
    api: HardnessAPI = None,
):
    """
    Sincroniza os Orçamentos do CRM (crm001GridPrincipalOrcamentos) do Hardness ERP em lote
    (bulk_create / bulk_update) para as empresas do grupo.
    """
    api = api or HardnessAPI()
    if not api.autenticado:
        api.login()

    total_criados = 0
    total_atualizados = 0

    empresas_para_rodar = api.empresas_dict.items()
    if empresa_nome and empresa_nome in api.empresas_dict:
        empresas_para_rodar = [(empresa_nome, api.empresas_dict[empresa_nome])]

    for nome_empresa, dados_empresa in empresas_para_rodar:
        empresa_id = dados_empresa["id_sistema"]
        print(f"\n📋 [Orçamentos CRM] Sincronizando: {nome_empresa} ({data_inicio} a {data_fim})")

        if not api.trocar_empresa(empresa_id):
            print(f"⚠️ Pulando {nome_empresa} (Orçamentos) por falha na troca de sessão.")
            continue

        if not api.filtrar_orcamentos(data_inicio=data_inicio, data_fim=data_fim):
            print(f"⚠️ Não foi possível aplicar filtro de Orçamentos em {nome_empresa}.")
            continue

        df_orc = api.get_dados(url=api.orcamentos_url)
        if df_orc.empty:
            print(f"ℹ️ Nenhum orçamento encontrado para {nome_empresa} no período.")
            continue

        if "Vendedor.C007_Primeiro_Nome" in df_orc.columns:
            vendedores_lote = df_orc["Vendedor.C007_Primeiro_Nome"].dropna().unique()
            cadastrar_vendedores_hardness(vendedores_lote)

        registros_preparados = {}
        for item in df_orc.to_dict("records"):
            num_orc = _limpar_str(item.get("T003_Id"))
            if not num_orc:
                continue

            flag_status = (_limpar_str(item.get("T003_Flag_Status_Orcamento"), max_len=10) or "").upper()
            flag_perdido = (_limpar_str(item.get("T003_Flag_Perdido"), max_len=10) or "").upper()

            if flag_status == "C":
                status_calc = "CANCELADO"
            elif flag_perdido == "S":
                status_calc = "PERDIDO"
            elif flag_status == "F" or flag_perdido == "F":
                status_calc = "FINALIZADO"
            else:
                status_calc = "PENDENTE"

            vend_interno = (
                _limpar_str(item.get("Vendedor.C007_Primeiro_Nome"), max_len=100) or "DESCONHECIDO"
            ).upper()
            vend_externo = _limpar_str(item.get("Externo.C007_Primeiro_Nome"), max_len=100)

            registros_preparados[num_orc] = {
                "empresa": nome_empresa,
                "data_emissao": parse_data(item.get("T003_Data_Emissao")),
                "hora_inclusao": _limpar_str(item.get("T003A_Hora_Inclusao"), max_len=20),
                "cliente_id_erp": _limpar_str(item.get("T003_D024_Id"), max_len=30),
                "cliente_nome": _limpar_str(
                    item.get("D024_Nome_Fantasia") or item.get("substr(D024_Nome_Empresa,1,15)"),
                    max_len=255,
                ),
                "contato": _limpar_str(item.get("Contato"), max_len=150),
                "cidade": _limpar_str(item.get("D020_Nome_Cidade"), max_len=100),
                "uf": _limpar_str(item.get("UFCliente.D018_UF"), max_len=10),
                "vendedor_nome": vend_interno,
                "vendedor_externo": vend_externo.upper() if vend_externo else None,
                "cfop": _limpar_str(item.get("D006_Codigo_CFOP"), max_len=20),
                "marca": _limpar_str(item.get("Marca"), max_len=120),
                "valor_produtos": parse_decimal(item.get("T003_Valor_Total_Produtos")),
                "valor_desconto": parse_decimal(item.get("T003_Valor_Desconto")),
                "valor_frete": parse_decimal(item.get("T003_Valor_Frete")),
                "valor_total": parse_decimal(item.get("T003_Valor_Total")),
                "valor_pendente": parse_decimal(item.get("T003_Valor_Pendente")),
                "valor_custo": parse_decimal(item.get("Total_Valor_Custo")),
                "valor_comissao": parse_decimal(item.get("Total_Valor_Comissao")),
                "percentual_margem": parse_decimal(item.get("T003_Percentual_Margem"), max_digits=14),
                "ipv": parse_decimal(item.get("T003_IPV"), casas=4, max_digits=14),
                "status": status_calc,
                "flag_status": flag_status or None,
                "flag_perdido": flag_perdido or None,
                "motivo_perda": _limpar_str(item.get("T003_Observacao_Orcamento_Perdido")),
                "pedido_gerado": _limpar_str(item.get("Pedido") or item.get("T005_Id"), max_len=100),
                "numero_nota": _limpar_str(item.get("NF"), max_len=50),
                "observacao": _limpar_str(item.get("T003_Observacao_1")),
                "dados_brutos": {k: (None if pd.isna(v) else v) for k, v in item.items()},
            }

        existentes_map = {
            obj.numero_orcamento: obj
            for obj in Orcamento.objects.filter(
                empresa=nome_empresa,
                numero_orcamento__in=list(registros_preparados.keys()),
            )
        }

        agora = timezone.now()
        para_criar = []
        para_atualizar = []
        campos_update = [
            "data_emissao",
            "hora_inclusao",
            "cliente_id_erp",
            "cliente_nome",
            "contato",
            "cidade",
            "uf",
            "vendedor_nome",
            "vendedor_externo",
            "cfop",
            "marca",
            "valor_produtos",
            "valor_desconto",
            "valor_frete",
            "valor_total",
            "valor_pendente",
            "valor_custo",
            "valor_comissao",
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
            "data_sincronizacao",
        ]

        for num_orc, defaults in registros_preparados.items():
            existente = existentes_map.get(num_orc)
            if existente:
                for campo, valor in defaults.items():
                    setattr(existente, campo, valor)
                existente.data_sincronizacao = agora
                para_atualizar.append(existente)
            else:
                para_criar.append(Orcamento(numero_orcamento=num_orc, **defaults))

        with transaction.atomic():
            if para_criar:
                Orcamento.objects.bulk_create(para_criar, batch_size=500)
            if para_atualizar:
                Orcamento.objects.bulk_update(para_atualizar, fields=campos_update, batch_size=500)

        c_emp = len(para_criar)
        a_emp = len(para_atualizar)
        print(f"✅ [Orçamentos] {nome_empresa}: {c_emp} criados, {a_emp} atualizados.")
        total_criados += c_emp
        total_atualizados += a_emp

    return total_criados, total_atualizados


def sincronizar_orcamentos_desde_ultimo_registro(
    empresa_nome: str = None,
    dias_retroativos_padrao: int = 15,
):
    """
    Sincroniza orçamentos de forma incremental (voltando `dias_retroativos_padrao` dias
    para atualizar orçamentos pendentes que foram ganhos/perdidos/cancelados), ou desde
    01/01 do ano atual caso a tabela esteja vazia.
    """
    ultima_data = Orcamento.objects.aggregate(Max("data_emissao"))["data_emissao__max"]
    hoje = timezone.now().date()

    if ultima_data:
        data_inicio_dt = ultima_data - timedelta(days=dias_retroativos_padrao)
    else:
        data_inicio_dt = date(hoje.year, 1, 1)

    return sincronizar_orcamentos_hardness(
        data_inicio=data_inicio_dt.strftime("%d/%m/%Y"),
        data_fim=hoje.strftime("%d/%m/%Y"),
        empresa_nome=empresa_nome,
    )