# integrations/piperun.py
import json
import time
from pathlib import Path
import requests

try:
    from decouple import config
    PIPERUN_API_BASE_URL = config("PIPERUN_API_BASE_URL", default="https://api.pipe.run/v1")
    PIPERUN_TOKEN = config("PIPERUN_TOKEN", default="")
except ImportError:
    PIPERUN_API_BASE_URL = "https://api.pipe.run/v1"
    PIPERUN_TOKEN = ""


class PipeRunAPI:
    def __init__(self, timeout: int = 20):
        self.base_url = (PIPERUN_API_BASE_URL or "https://api.pipe.run/v1").rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.headers = {
            "token": PIPERUN_TOKEN,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.session.headers.update(self.headers)

    def _get(self, endpoint: str, params=None, tentativas: int = 2) -> dict:
        if not PIPERUN_TOKEN:
            return {"error": "PIPERUN_TOKEN não configurado no .env."}

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        ultimo_erro = None

        for tentativa in range(tentativas):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code in (429, 502, 503, 504) and tentativa + 1 < tentativas:
                    time.sleep(1.2 * (tentativa + 1))
                    continue
                return self._handle_response(response)
            except requests.RequestException as exc:
                ultimo_erro = str(exc)
                time.sleep(1.0 * (tentativa + 1))

        return {"error": f"Falha de rede na API PipeRun ({endpoint}): {ultimo_erro}"}

    def _handle_response(self, response: requests.Response) -> dict:
        if response.status_code == 200:
            try:
                data = response.json()
                return data if isinstance(data, dict) else {"data": data}
            except ValueError:
                return {"error": "Resposta JSON inválida da API PipeRun", "message": response.text[:300]}
        elif response.status_code == 401:
            return {"error": "Token do PipeRun inválido ou expirado (401)."}
        elif response.status_code == 403:
            return {"error": "Acesso negado ao endpoint no PipeRun (403)."}
        elif response.status_code == 404:
            return {"error": "Endpoint ou recurso não encontrado no PipeRun (404)."}
        else:
            return {"error": f"Erro HTTP {response.status_code}", "message": response.text[:300]}

    def get_users(self, show: int = 200) -> dict:
        """Retorna todos os usuários ativos/cadastrados no PipeRun (suportando paginação)."""
        todos_usuarios = []
        pagina = 1
        while pagina <= 10:
            resp = self._get("users/", params={"show": show, "page": pagina})
            if not isinstance(resp, dict) or "data" not in resp:
                if pagina == 1:
                    return resp
                break

            lote = resp.get("data") or []
            if not isinstance(lote, list) or not lote:
                break

            todos_usuarios.extend(lote)
            meta = resp.get("meta") or {}
            total_pages = meta.get("total_pages") or meta.get("last_page") or 1
            if pagina >= int(total_pages) or len(lote) < show:
                break
            pagina += 1

        return {"data": todos_usuarios}

    def get_goals(self, user_id=None, show: int = 200) -> dict:
        """Busca metas avançadas (advanced-goals) com fallback para paginação."""
        todas_metas = []
        pagina = 1
        while pagina <= 10:
            params = {"show": show, "page": pagina}
            if user_id:
                params["user_id"] = user_id

            resp = self._get("advanced-goals/", params=params)
            if not isinstance(resp, dict) or "data" not in resp:
                if pagina == 1:
                    return resp
                break

            lote = resp.get("data") or []
            if not isinstance(lote, list) or not lote:
                break

            todas_metas.extend(lote)
            meta = resp.get("meta") or {}
            total_pages = meta.get("total_pages") or meta.get("last_page") or 1
            if pagina >= int(total_pages) or len(lote) < show:
                break
            pagina += 1

        return {"data": todas_metas}

    def get_goal_stats(self, goal_id) -> dict:
        return self._get(f"advanced-goals/{goal_id}/stats")

    def _extrair_valor_stats(self, stats_obj) -> float:
        """
        Extrai o valor numérico da meta a partir da resposta do endpoint /stats.
        Suporta diferentes versões de schema da API do PipeRun.
        """
        if isinstance(stats_obj, dict):
            for k in ["target", "target_value", "value", "valor", "goal_value", "expected"]:
                if k in stats_obj and isinstance(stats_obj[k], (int, float, str)):
                    try:
                        val = float(stats_obj[k])
                        if val > 0:
                            return val
                    except (ValueError, TypeError):
                        pass

            for k, v in stats_obj.items():
                if k in ["error", "status", "code", "message", "user_id", "id"]:
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
                data_inicio = goal.get("start_date") or goal.get("start_at") or goal.get("created_at")
                data_fim = goal.get("due_date") or goal.get("end_date") or goal.get("end_at")

                stats = self.get_goal_stats(goal_id)
                valor_meta = self._extrair_valor_stats(stats) if not stats.get("error") else 0.0

                valor_total_usuario += valor_meta
                metas_usuario.append(
                    {
                        "goal_id": goal_id,
                        "goal_title": goal_title,
                        "data_inicio": data_inicio,
                        "data_fim": data_fim,
                        "valor": valor_meta,
                    }
                )

            if metas_usuario:
                resultado.append(
                    {
                        "user_id": user_id,
                        "nome": nome_usuario,
                        "total_metas": len(metas_usuario),
                        "valor_total": valor_total_usuario,
                        "metas": metas_usuario,
                    }
                )

        if salvar_json and resultado:
            data_dir = Path(__file__).resolve().parent / "data"
            data_dir.mkdir(exist_ok=True)
            arquivo_destino = data_dir / "metas_por_vendedores.json"
            with open(arquivo_destino, "w", encoding="utf-8") as f:
                json.dump(resultado, f, indent=2, ensure_ascii=False)
            print(f"📁 JSON salvo em: {arquivo_destino}")

        return resultado