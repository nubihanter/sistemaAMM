import re
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from django.utils import timezone


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


class ConsultaCAClient:
    """
    Cliente de consulta de Certificado de Aprovação (CA - MTE) via portal ConsultaCA.
    Extrai data de validade real, status (VÁLIDO, VENCIDO, SUSPENSO, CANCELADO),
    fabricante/importador e descrição técnica do equipamento.
    """

    BASE_URL = "https://consultaca.com"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    @staticmethod
    def limpar_numero_ca(numero_ca: str) -> str:
        apenas_digitos = re.sub(r"\D", "", str(numero_ca or ""))
        return apenas_digitos.lstrip("0") or apenas_digitos

    def consultar_ca(self, numero_ca: str, tentativas: int = 2) -> dict:
        ca_limpo = self.limpar_numero_ca(numero_ca)
        if not ca_limpo:
            return {"encontrado": False, "numero_ca": str(numero_ca), "erro": "Número de CA vazio"}

        url = f"{self.BASE_URL}/{ca_limpo}"
        ultimo_erro = None

        for tentativa in range(tentativas):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 404:
                    return {"encontrado": False, "numero_ca": ca_limpo, "erro": "CA não encontrado (404)"}
                if resp.status_code != 200:
                    ultimo_erro = f"HTTP {resp.status_code}"
                    time.sleep(0.8 * (tentativa + 1))
                    continue

                return self._parse_html(ca_limpo, resp.text)
            except requests.RequestException as exc:
                ultimo_erro = str(exc)
                time.sleep(0.8 * (tentativa + 1))

        return {
            "encontrado": False,
            "numero_ca": ca_limpo,
            "erro": ultimo_erro or "Falha desconhecida na consulta",
        }

    def _parse_html(self, ca_limpo: str, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")

        # Verifica se a página realmente contém o bloco de identificação do CA
        tag_num_ca = soup.find("p", class_="num_ca")
        if not tag_num_ca:
            return {
                "encontrado": False,
                "numero_ca": ca_limpo,
                "erro": "CA não localizado na base do MTE/ConsultaCA",
            }

        data_validade = None
        status = "VALIDO"
        fabricante = None
        nome_fantasia = None
        descricao_equipamento = None

        # Título principal do equipamento no MTE
        h1_tag = soup.find("h1")
        titulo_equipamento = h1_tag.get_text(" ", strip=True) if h1_tag else ""
        if titulo_equipamento and "Avalie este EPI" in titulo_equipamento:
            titulo_equipamento = ""

        paragrafos = soup.find_all("p")
        for p in paragrafos:
            texto = p.get_text(" ", strip=True)
            if not texto:
                continue

            if texto.startswith("Situação:"):
                sit_raw = texto.replace("Situação:", "").strip().upper()
                if "VENCIDO" in sit_raw:
                    status = "VENCIDO"
                elif "SUSPENSO" in sit_raw:
                    status = "SUSPENSO"
                elif "CANCELADO" in sit_raw:
                    status = "CANCELADO"
                elif "VÁLIDO" in sit_raw or "VALIDO" in sit_raw:
                    status = "VALIDO"

            elif texto.startswith("Validade:"):
                m_data = re.search(r"(\d{2}/\d{2}/\d{4})", texto)
                if m_data:
                    try:
                        data_validade = datetime.strptime(m_data.group(1), "%d/%m/%Y").date()
                    except ValueError:
                        data_validade = None

            elif texto.startswith("Razão Social Importador:") and not fabricante:
                fabricante = texto.replace("Razão Social Importador:", "").strip()

            elif texto.startswith("Razão Social:") and not fabricante:
                fabricante = texto.replace("Razão Social:", "").strip()

            elif texto.startswith("Nome Fantasia:") and not nome_fantasia:
                nome_fantasia = texto.replace("Nome Fantasia:", "").strip()

        # Descrição técnica detalhada (segundo <p class="info"> quando disponível)
        infos = soup.find_all("p", class_="info")
        detalhe_tecnico = ""
        for p_info in infos:
            t_info = p_info.get_text(" ", strip=True)
            if (
                t_info
                and not any(
                    t_info.startswith(prefix)
                    for prefix in (
                        "Razão Social",
                        "CNPJ",
                        "Nome Fantasia:",
                        "Cidade/UF:",
                        "Avaliação Geral:",
                        "Marcação:",
                        "Referências:",
                        "Cor:",
                        "Restrições:",
                        "N° do Laudo:",
                    )
                )
                and len(t_info) > 25
            ):
                detalhe_tecnico = t_info
                break

        if titulo_equipamento and detalhe_tecnico:
            descricao_equipamento = f"{titulo_equipamento} — {detalhe_tecnico}"
        elif titulo_equipamento:
            descricao_equipamento = titulo_equipamento
        elif detalhe_tecnico:
            descricao_equipamento = detalhe_tecnico

        # Garante consistência entre a data de validade e o status
        hoje = timezone.now().date()
        if data_validade:
            if data_validade < hoje and status == "VALIDO":
                status = "VENCIDO"

        return {
            "encontrado": True,
            "numero_ca": ca_limpo,
            "data_validade": data_validade,
            "status": status,
            "fabricante": fabricante or nome_fantasia,
            "descricao_equipamento": descricao_equipamento,
        }


def consultar_ca_epi(numero_ca: str) -> dict:
    """Função utilitária rápida para consultar um único CA."""
    client = ConsultaCAClient()
    return client.consultar_ca(numero_ca)
