# integrations/pipeRunLib.py
import os
import sys
import json
from pathlib import Path
import requests

# Importa as configurações do sistema
try:
    from decouple import config
    PIPERUN_API_BASE_URL = config("PIPERUN_API_BASE_URL", default="https://api.pipe.run/v1")
    PIPERUN_TOKEN = config("PIPERUN_TOKEN", default="")
except ImportError:
    from config import PIPERUN_API_BASE_URL, PIPERUN_TOKEN


class PipeRunAPI:
    def __init__(self):
        self.base_url = PIPERUN_API_BASE_URL.rstrip("/")
        self.headers = {
            "token": PIPERUN_TOKEN,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def _get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint}"
        response = requests.get(url, headers=self.headers, params=params)
        return self._handle_response(response)

    def _handle_response(self, response):
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 401:
            return {"error": "Token inválido ou expirado."}
        else:
            return {"error": f"Erro {response.status_code}", "message": response.text}

    def get_users(self):
        return self._get("users/")

    def get_goals(self, user_id=None, show=50):
        params = {"user_id": user_id} if user_id else {}
        params["show"] = show
        return self._get("advanced-goals/", params=params)

    def get_goal_stats(self, goal_id):
        return self._get(f"advanced-goals/{goal_id}/stats")

    def _extrair_valor_stats(self, stats_obj):
        """
        Extrai o valor numérico da meta a partir da resposta do endpoint /stats,
        conforme a lógica recursiva testada no __main__.
        """
        if isinstance(stats_obj, dict):
            # 1. Procura chaves diretas conhecidas
            for k in ["target", "target_value", "value", "valor", "goal_value"]:
                if k in stats_obj and isinstance(stats_obj[k], (int, float, str)):
                    try:
                        val = float(stats_obj[k])
                        if val > 0:
                            return val
                    except (ValueError, TypeError):
                        pass

            # 2. Busca recursiva ignorando metadados de requisição
            for k, v in stats_obj.items():
                if k in ["error", "status", "code", "message"]:
                    continue
                val = self._extrair_valor_stats(v)
                if val > 0:
                    return val

        elif isinstance(stats_obj, list):
            for item in stats_obj:
                val = self._extrair_valor_stats(item)
                if val > 0:
                    return val

        return 0.0

    def export_goals_by_seller(self, salvar_json=True):
        """
        Percorre os usuários do PipeRun, busca suas metas e monta a estrutura completa.
        Salva em data/metas_por_vendedores.json se solicitado.
        """
        users_resp = self.get_users()
        if not isinstance(users_resp, dict) or "data" not in users_resp:
            print(f"❌ Erro ao buscar usuários: {users_resp.get('error', 'Sem resposta')}")
            return []

        resultado = []

        for user in users_resp["data"]:
            user_id = user.get("id")
            nome_usuario = user.get("name", "")

            goals_resp = self.get_goals(user_id=user_id, show=100)
            if not isinstance(goals_resp, dict) or "data" not in goals_resp:
                continue

            metas_usuario = []
            valor_total_usuario = 0.0

            for goal in goals_resp["data"]:
                goal_id = goal.get("id")
                goal_title = goal.get("title", "")
                data_inicio = goal.get("start_date") or goal.get("created_at")
                data_fim = goal.get("due_date") or goal.get("end_date")

                # Busca valor via endpoint /stats validado
                stats = self.get_goal_stats(goal_id)
                valor_meta = self._extrair_valor_stats(stats) if not stats.get("error") else 0.0

                valor_total_usuario += valor_meta
                metas_usuario.append({
                    "goal_id": goal_id,
                    "goal_title": goal_title,
                    "data_inicio": data_inicio,
                    "data_fim": data_fim,
                    "valor": valor_meta
                })

            if metas_usuario:
                resultado.append({
                    "user_id": user_id,
                    "nome": nome_usuario,
                    "total_metas": len(metas_usuario),
                    "valor_total": valor_total_usuario,
                    "metas": metas_usuario
                })

        # Salva o arquivo json no diretório data/ se habilitado
        if salvar_json and resultado:
            data_dir = Path(__file__).resolve().parent / "data"
            data_dir.mkdir(exist_ok=True)
            arquivo_destino = data_dir / "metas_por_vendedores.json"
            with open(arquivo_destino, "w", encoding="utf-8") as f:
                json.dump(resultado, f, indent=2, ensure_ascii=False)
            print(f"📁 JSON salvo em: {arquivo_destino}")

        return resultado


if __name__ == "__main__":
    api = PipeRunAPI()
    print("🚀 Testando export_goals_by_seller()...")
    dados = api.export_goals_by_seller(salvar_json=True)
    print(f"✅ Concluído! Vendedores encontrados: {len(dados)}")