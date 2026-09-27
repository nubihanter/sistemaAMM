import re
import json
import time
import requests
from bs4 import BeautifulSoup
import pandas as pd
from decouple import config

HARDNESS_USER = config("HARDNESS_USER", default="")
HARDNESS_PASSWORD = config("HARDNESS_PASSWORD", default="")
HARDNESS_BASE_URL = config("HARDNESS_BASE_URL", default="").rstrip("/")
HARDNESS_NOTAFISCAL_GRID_ID = config("HARDNESS_NOTAFISCAL_GRID_ID", default="")
HARDNESS_PRODUTOS_GRID_ID = config("HARDNESS_PRODUTOS_GRID_ID", default="")
HARDNESS_ESTOQUE_GRID_ID = config("HARDNESS_ESTOQUE_GRID_ID", default="")


class HardnessAPIError(Exception):
    """Exceção específica para falhas de comunicação ou scraping no Hardness ERP."""


class HardnessAPI:
    def __init__(self, verbose=True, timeout=35):
        self.name = "AMM"
        self.timeout = timeout
        self.empresas_dict = {
            "AMM EPIS": {"id_sistema": "1"},
            "AMM Solucoes": {"id_sistema": "2"},
        }
        self.session = requests.Session()
        self.base_url = HARDNESS_BASE_URL
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/erp/",
            }
        )
        self.url_login = f"{self.base_url}/hardness3/outros/login/index.php"
        self.notafiscal_url = f"{self.base_url}/crm/crm001/grid/crm001GridPrincipalNotasFiscais/"
        self.produtos_url = f"{self.base_url}/crm/crm001/grid/crm001gridPrincipalProdutos/"
        self.estoque_url = f"{self.base_url}/cad/cad002/grid/lista/"
        self.verbose = verbose
        self.autenticado = False
        self.grid_dicts = {
            self.notafiscal_url: HARDNESS_NOTAFISCAL_GRID_ID,
            self.produtos_url: HARDNESS_PRODUTOS_GRID_ID,
            self.estoque_url: HARDNESS_ESTOQUE_GRID_ID,
        }

    def _request(self, method: str, url: str, tentativas: int = 2, **kwargs):
        """Executa requisição HTTP com timeout padrão e retry automático em falhas de rede."""
        kwargs.setdefault("timeout", self.timeout)
        ultimo_erro = None
        for t in range(tentativas):
            try:
                resp = self.session.request(method, url, **kwargs)
                return resp
            except requests.RequestException as exc:
                ultimo_erro = exc
                if self.verbose:
                    print(f"⚠️ [Hardness] Falha de conexão ({t + 1}/{tentativas}) em {url}: {exc}")
                time.sleep(1.0 * (t + 1))
        raise HardnessAPIError(f"Falha de comunicação com o servidor Hardness ({url}): {ultimo_erro}")

    def login(self) -> bool:
        if not self.base_url or not HARDNESS_USER or not HARDNESS_PASSWORD:
            raise HardnessAPIError(
                "Credenciais do Hardness (HARDNESS_BASE_URL, HARDNESS_USER, HARDNESS_PASSWORD) não configuradas no .env."
            )
        if self.verbose:
            print("🔐 Fazendo login no sistema Hardness...")
        payload_login = {
            "login": HARDNESS_USER,
            "senha": HARDNESS_PASSWORD,
        }
        resp = self._request("POST", self.url_login, data=payload_login)
        if resp.status_code >= 400:
            raise HardnessAPIError(f"Erro HTTP {resp.status_code} ao tentar autenticar no Hardness.")
        self.autenticado = True
        if self.verbose:
            print("✅ Login efetuado no Hardness.")
        return True

    def trocar_empresa(self, empresa_id) -> bool:
        if not self.autenticado:
            self.login()

        url_troca_empresa = f"{self.base_url}/empresa/{empresa_id}/"
        if self.verbose:
            print(f"🔄 Trocando sessão para a empresa ID {empresa_id}...")

        resposta_troca = self._request("POST", url_troca_empresa)
        verifica_empresa = self.verifica_empresa()

        # Se a sessão tiver expirado, refaz o login uma vez e tenta novamente
        if str(verifica_empresa) != str(empresa_id):
            if self.verbose:
                print("🔄 Sessão expirada ou empresa divergente. Reautenticando no Hardness...")
            self.login()
            resposta_troca = self._request("POST", url_troca_empresa)
            verifica_empresa = self.verifica_empresa()

        if str(verifica_empresa) == str(empresa_id):
            if self.verbose:
                print("✅ Empresa trocada com sucesso no servidor.")
            return True
        else:
            print(
                f"⚠️ Atenção: A troca de empresa retornou status {resposta_troca.status_code} "
                f"(empresa verificada: {verifica_empresa}, esperada: {empresa_id})"
            )
            return False

    def verifica_empresa(self):
        url = (
            f"{self.base_url}/sistema/funcoes/util/verificaEmpresaAtual/"
            "?ajax=true&callback=jQuery16205376931034128021_1778415793103&_=1778415835153"
        )
        resposta = self._request("GET", url)
        texto = resposta.text.strip()
        match = re.search(r"^[^\(]+\((.*)\);?$", texto, re.DOTALL)

        if match:
            dado_limpo = match.group(1).strip().strip('"').strip("'")
            if self.verbose:
                print(f"   ℹ️ Empresa ativa na sessão: {dado_limpo}")
            return dado_limpo
        elif texto.isdigit():
            return texto
        else:
            # NUNCA usar input() aqui para não travar threads web ou agendador em background!
            print(f"⚠️ Atenção: Não foi possível extrair o ID da empresa atual (resposta: {texto[:120]}).")
            return None

    @staticmethod
    def _extrair_grid_hash(html_text: str) -> str:
        """Extrai o hash MD5 de 32 caracteres do grid no HTML/JS do Hardness."""
        padroes = [
            r"encodeURIComponent\(['\"]([a-f0-9]{32})['\"]\)",
            r"grid['\"]?\s*[:=]\s*['\"]([a-f0-9]{32})['\"]",
            r"\b([a-f0-9]{32})\b",
        ]
        for padrao in padroes:
            m = re.search(padrao, html_text, re.IGNORECASE)
            if m:
                return m.group(1)
        return ""

    def filtrar(self, data_inicio="", data_fim="", CFOP="VENDA", cancelada="N", url=None):
        alvo_url = url or self.notafiscal_url
        payload = {
            "ajax": "true",
            "divIdRoot": "crm001",
            "tab": "geral",
        }

        self._request("POST", alvo_url, data=payload)
        response_pagina = self._request("GET", alvo_url, params=payload)

        soup = BeautifulSoup(response_pagina.text, "html.parser")
        form = soup.select_one("div.gridFiltro form")

        if not form:
            print("⚠️ Formulário de filtro não encontrado na primeira tentativa. Reautenticando...")
            self.login()
            self._request("POST", alvo_url, data=payload)
            response_pagina = self._request("GET", alvo_url, params=payload)
            soup = BeautifulSoup(response_pagina.text, "html.parser")
            form = soup.select_one("div.gridFiltro form")
            if not form:
                print("❌ Formulário de filtro não encontrado no Hardness!")
                return False

        form_id = form.get("id", "")
        if self.verbose:
            print(f"Form ID encontrado: {form_id}")

        filter_data = {}
        for input_tag in form.find_all("input"):
            name = input_tag.get("name", "")
            if not name:
                continue

            if name.endswith("-titulo"):
                titulo = input_tag.get("value", "").lower()
                base64_name = name.replace("-titulo", "")
                if "cfop" in titulo:
                    filter_data[base64_name] = CFOP
                elif "cancelada" in titulo:
                    filter_data[base64_name] = cancelada
            elif name.endswith("-d1") and data_inicio:
                filter_data[name] = data_inicio
            elif name.endswith("-d2") and data_fim:
                filter_data[name] = data_fim

        for input_tag in form.find_all(["input", "select"]):
            name = input_tag.get("name")
            if name and name not in filter_data:
                if not name.endswith("-titulo") and "VDAwN19EYXRhX0VtaXNzYW8=" not in name:
                    filter_data[name] = ""

        grid_hash = self._extrair_grid_hash(response_pagina.text) or self.grid_dicts.get(alvo_url, "")
        if not grid_hash:
            print("⚠️ Aviso: Não encontrou o hash do grid. O filtro vai falhar.")
            return False

        post_data = {
            "ajax": "true",
            "filtroUID": form_id,
            "grid": grid_hash,
        }
        post_data.update(filter_data)

        full_url = f"{self.base_url}/sistema/funcoes/gridFiltro/filtrar/"
        response_filter = self._request("POST", full_url, data=post_data)

        if response_filter.status_code == 200:
            if self.verbose:
                print("✅ Filtro aplicado com sucesso!")
            self._request("GET", alvo_url, params={"ajax": "true"})
            return True
        else:
            print(f"❌ Erro ao aplicar filtro: {response_filter.status_code}")
            return False

    def get_dados(self, url=None, max_paginas=100):
        alvo_url = url or self.notafiscal_url
        todos_dados_empresa = []
        loading_offset = 0
        pagina = 0

        while pagina < max_paginas:
            payload_grid = {
                "ajax": "true",
                "tab": "geral",
                "gridFiltrado": "true",
                "limit": "5000",
                "limite": "5000",
                "rows": "5000",
                "length": "5000",
            }
            if loading_offset > 0:
                payload_grid["loading"] = str(pagina)

            if self.verbose:
                print(f"📥 Baixando página {pagina} (A partir da linha {loading_offset})...")
            response = self._request("POST", alvo_url, data=payload_grid)

            soup = BeautifulSoup(response.text, "html.parser")
            linhas_com_dados = soup.find_all("tr", attrs={"todoscampos": True})

            if not linhas_com_dados:
                if self.verbose:
                    print("🏁 Fim dos dados retornado pelo servidor!")
                break

            if self.verbose:
                print(f"   ✅ Encontrados {len(linhas_com_dados)} registros nesta página.")

            for linha in linhas_com_dados:
                json_texto = linha.get("todoscampos")
                if json_texto:
                    try:
                        dados_linha = json.loads(json_texto)
                        todos_dados_empresa.append(dados_linha)
                    except json.JSONDecodeError:
                        continue

            loading_offset += len(linhas_com_dados)
            pagina += 1

        df = pd.DataFrame(todos_dados_empresa)
        if "excluirLinha" in df.columns:
            df = df.drop(columns=["excluirLinha"])
        return df

    def filtrar_estoque(self, codigo="!"):
        """
        Aplica o filtro na página de estoque, limpando todos os outros campos
        e inserindo "!" (não vazio) no campo Código.
        """
        url = self.estoque_url
        payload = {
            "ajax": "true",
            "divIdRoot": "crm001",
            "tab": "geral",
        }

        self._request("POST", url, data=payload)
        response_pagina = self._request("GET", url, params=payload)

        soup = BeautifulSoup(response_pagina.text, "html.parser")
        form = soup.select_one("div.gridFiltro form")
        if not form:
            print("⚠️ Formulário de filtro de estoque não encontrado! Tentando reautenticar...")
            self.login()
            self.trocar_empresa("1")
            self._request("POST", url, data=payload)
            response_pagina = self._request("GET", url, params=payload)
            soup = BeautifulSoup(response_pagina.text, "html.parser")
            form = soup.select_one("div.gridFiltro form")
            if not form:
                print("❌ Formulário de filtro de estoque não encontrado!")
                return False

        form_id = form.get("id", "")
        filter_data = {}

        for input_tag in form.find_all("input"):
            name = input_tag.get("name", "")
            if not name:
                continue

            if name.endswith("-titulo"):
                titulo = input_tag.get("value", "").lower()
                base64_name = name.replace("-titulo", "")
                if "código" in titulo or "codigo" in titulo:
                    filter_data[base64_name] = codigo

        for input_tag in form.find_all(["input", "select"]):
            name = input_tag.get("name")
            if name and name not in filter_data:
                if not name.endswith("-titulo"):
                    filter_data[name] = ""

        grid_hash = self._extrair_grid_hash(response_pagina.text) or self.grid_dicts.get(url, "")
        if not grid_hash:
            print("⚠️ Aviso: Não encontrou o hash do grid de estoque.")
            return False

        post_data = {
            "ajax": "true",
            "filtroUID": form_id,
            "grid": grid_hash,
        }
        post_data.update(filter_data)

        full_url = f"{self.base_url}/sistema/funcoes/gridFiltro/filtrar/"
        response_filter = self._request("POST", full_url, data=post_data)

        if response_filter.status_code == 200:
            if self.verbose:
                print("✅ Filtro de estoque aplicado com sucesso!")
            self._request("GET", url, params={"ajax": "true"})
            return True
        else:
            print(f"❌ Erro ao aplicar filtro no estoque: {response_filter.status_code}")
            return False

    def get_dados_estoque(self, max_paginas=100):
        """
        Extrai os dados paginados da url de estoque e retorna um DataFrame limpo.
        """
        url = self.estoque_url
        todos_dados_estoque = []
        loading_offset = 0
        pagina = 0

        while pagina < max_paginas:
            payload_grid = {
                "ajax": "true",
                "tab": "geral",
                "gridFiltrado": "true",
                "limit": "5000",
                "limite": "5000",
                "rows": "5000",
                "length": "5000",
            }
            if loading_offset > 0:
                payload_grid["loading"] = str(pagina)

            if self.verbose:
                print(f"📥 Baixando página de estoque {pagina} (A partir da linha {loading_offset})...")
            response = self._request("POST", url, data=payload_grid)

            soup = BeautifulSoup(response.text, "html.parser")
            linhas_com_dados = soup.find_all("tr", attrs={"todoscampos": True})

            if not linhas_com_dados:
                if self.verbose:
                    print("🏁 Fim dos dados retornados pelo servidor!")
                break

            if self.verbose:
                print(f"   ✅ Encontrados {len(linhas_com_dados)} itens nesta página.")

            for linha in linhas_com_dados:
                json_texto = linha.get("todoscampos")
                if json_texto:
                    try:
                        dados_linha = json.loads(json_texto)
                        todos_dados_estoque.append(dados_linha)
                    except json.JSONDecodeError:
                        continue

            loading_offset += len(linhas_com_dados)
            pagina += 1

        df = pd.DataFrame(todos_dados_estoque)
        if df.empty:
            print("ℹ️ Nenhum dado encontrado no estoque com esse filtro.")
            return df

        if "excluirLinha" in df.columns:
            df = df.drop(columns=["excluirLinha"])

        return df
