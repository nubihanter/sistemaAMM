from django.db import models

class NotaFiscal(models.Model):
    empresa = models.CharField(max_length=100, verbose_name="Empresa")
    numero_nota = models.CharField(max_length=50, unique=True, verbose_name="Número da NF")
    serie = models.CharField(max_length=20, blank=True, null=True, verbose_name="Série")
    cliente_nome = models.CharField(max_length=255, blank=True, null=True, verbose_name="Cliente / Razão Social")
    cliente_documento = models.CharField(max_length=30, blank=True, null=True, verbose_name="CNPJ/CPF")
    data_emissao = models.DateField(blank=True, null=True, verbose_name="Data de Emissão")
    valor_total = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Total")
    cfop = models.CharField(max_length=10, blank=True, null=True, verbose_name="CFOP")
    status = models.CharField(max_length=50, blank=True, null=True, verbose_name="Status")
    vendedor_nome = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="Vendedor")
    
    # Campo JSON para armazenar todos os campos brutos vindos do Hardness (evita perder dados)
    dados_brutos = models.JSONField(blank=True, null=True, verbose_name="Payload Bruto")
    data_sincronizacao = models.DateTimeField(auto_now=True, verbose_name="Última Sincronização")

    class Meta:
        verbose_name = "Nota Fiscal"
        verbose_name_plural = "Notas Fiscais"
        ordering = ["-data_emissao", "-numero_nota"]

    def __str__(self):
        return f"NF {self.numero_nota} - {self.cliente_nome}"

# sales/models.py (adicione ao final do arquivo)

class MetaVendedor(models.Model):
    vendedor_nome = models.CharField(max_length=100, db_index=True, verbose_name="Vendedor / Empresa")
    mes = models.PositiveSmallIntegerField(verbose_name="Mês (1-12)")
    ano = models.PositiveIntegerField(verbose_name="Ano")
    valor = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor da Meta (R$)")
    titulo_meta = models.CharField(max_length=255, blank=True, null=True, verbose_name="Título / Descrição da Meta")
    data_atualizacao = models.DateTimeField(auto_now=True, verbose_name="Última Atualização")

    class Meta:
        verbose_name = "Meta de Vendedor"
        verbose_name_plural = "Metas de Vendedores"
        unique_together = ("vendedor_nome", "mes", "ano")
        ordering = ["-ano", "-mes", "vendedor_nome"]

    def __str__(self):
        return f"{self.vendedor_nome} - {self.mes:02d}/{self.ano}: R$ {self.valor:,.2f}"

class Vendedor(models.Model):
    nome_hardness = models.CharField(
        max_length=100, 
        unique=True, 
        db_index=True, 
        verbose_name="Nome no Hardness (ERP)"
    )
    nome_piperun = models.CharField(
        max_length=100, 
        blank=True, 
        null=True, 
        verbose_name="Nome no PipeRun (CRM)",
        help_text="Se vazio, o sistema tentará o vínculo automático na próxima sincronização."
    )
    ativo = models.BooleanField(
        default=True, 
        verbose_name="Ativo no Dashboard (Seletor)"
    )
    ativo_ranking = models.BooleanField(
        default=True, 
        verbose_name="Ativo no Ranking"
    )

    class Meta:
        verbose_name = "Vendedor (Vínculo)"
        verbose_name_plural = "Vendedores (Vínculos)"
        ordering = ["nome_hardness"]

    def __str__(self):
        piperun_str = self.nome_piperun or "⚠️ SEM VÍNCULO"
        return f"{self.nome_hardness} ➔ {piperun_str}"