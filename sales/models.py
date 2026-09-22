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
    
    # Campo JSON para armazenar todos os campos brutos vindos do Hardness (evita perder dados)
    dados_brutos = models.JSONField(blank=True, null=True, verbose_name="Payload Bruto")
    data_sincronizacao = models.DateTimeField(auto_now=True, verbose_name="Última Sincronização")

    class Meta:
        verbose_name = "Nota Fiscal"
        verbose_name_plural = "Notas Fiscais"
        ordering = ["-data_emissao", "-numero_nota"]

    def __str__(self):
        return f"NF {self.numero_nota} - {self.cliente_nome}"