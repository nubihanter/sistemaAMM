from django.db import models

class NotaFiscal(models.Model):
    empresa = models.CharField(max_length=100, db_index=True, verbose_name="Empresa")
    numero_nota = models.CharField(max_length=50, db_index=True, verbose_name="Número da NF")
    serie = models.CharField(max_length=20, blank=True, null=True, verbose_name="Série")
    cliente_nome = models.CharField(max_length=255, blank=True, null=True, verbose_name="Cliente / Razão Social")
    cliente_documento = models.CharField(max_length=30, blank=True, null=True, db_index=True, verbose_name="CNPJ/CPF")
    data_emissao = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Emissão")
    valor_total = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Total")
    cfop = models.CharField(max_length=10, blank=True, null=True, verbose_name="CFOP")
    status = models.CharField(max_length=50, blank=True, null=True, db_index=True, verbose_name="Status")
    vendedor_nome = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="Vendedor")
    
    # Campo JSON para armazenar todos os campos brutos vindos do Hardness (evita perder dados)
    dados_brutos = models.JSONField(blank=True, null=True, verbose_name="Payload Bruto")
    data_sincronizacao = models.DateTimeField(auto_now=True, verbose_name="Última Sincronização")

    class Meta:
        verbose_name = "Nota Fiscal"
        verbose_name_plural = "Notas Fiscais"
        unique_together = ("empresa", "numero_nota")
        ordering = ["-data_emissao", "-numero_nota"]
        indexes = [
            models.Index(fields=["status", "data_emissao"]),
            models.Index(fields=["vendedor_nome", "data_emissao"]),
        ]

    def __str__(self):
        return f"NF {self.numero_nota} ({self.empresa}) - {self.cliente_nome}"

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


from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model


def garantir_usuario_para_vendedor(nome_hardness: str, senha_padrao: str = "amm@2026"):
    """
    Garante que o vendedor possua uma conta de usuário atrelada (via nome_vendedor_erp).
    Se não existir, cria automaticamente com perfil VENDEDOR e senha 'amm@2026'.
    """
    nome_limpo = str(nome_hardness or "").strip().upper()
    if not nome_limpo or nome_limpo in ("NAN", "NONE", "DESCONHECIDO"):
        return None, False

    User = get_user_model()

    # 1. Já existe usuário atrelado pelo campo nome_vendedor_erp?
    user_vinculado = User.objects.filter(nome_vendedor_erp__iexact=nome_limpo).first()
    if user_vinculado:
        return user_vinculado, False

    # 2. Já existe usuário com o mesmo username mas sem o vínculo preenchido?
    user_existente = User.objects.filter(username__iexact=nome_limpo).first()
    if user_existente:
        user_existente.nome_vendedor_erp = nome_limpo
        if not user_existente.first_name:
            user_existente.first_name = nome_limpo.title()
        user_existente.save(update_fields=["nome_vendedor_erp", "first_name"])
        return user_existente, False

    # 3. Cria novo usuário automaticamente com a senha padrão "amm@2026"
    novo_user = User(
        username=nome_limpo,
        first_name=nome_limpo.title(),
        role=User.Role.VENDEDOR,
        nome_vendedor_erp=nome_limpo,
        is_active=True,
    )
    novo_user.set_password(senha_padrao)
    novo_user.save()
    return novo_user, True


@receiver(post_save, sender=Vendedor)
def criar_usuario_vendedor_signal(sender, instance, **kwargs):
    garantir_usuario_para_vendedor(instance.nome_hardness, senha_padrao="amm@2026")


class LogSincronizacao(models.Model):
    TIPO_CHOICES = [
        ("NOTAS_HARDNESS", "Notas Fiscais (Hardness)"),
        ("ESTOQUE_HARDNESS", "Estoque & Itens (Hardness)"),
        ("METAS_PIPERUN", "Metas de Vendas (PipeRun)"),
        ("CONSULTA_CA", "Vencimentos de CA (MTE)"),
        ("COMPLETA", "Sincronização Completa"),
    ]
    ORIGEM_CHOICES = [
        ("MANUAL_PAINEL", "Painel Web (Manual)"),
        ("AGENDADOR_AUTO", "Agendador Automático"),
        ("CLI", "Comando de Terminal (CLI)"),
    ]
    STATUS_CHOICES = [
        ("EM_ANDAMENTO", "Em Andamento"),
        ("SUCESSO", "Sucesso"),
        ("ERRO", "Erro / Falha"),
    ]

    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES, db_index=True, verbose_name="Tipo de Sincronização")
    origem = models.CharField(max_length=20, choices=ORIGEM_CHOICES, default="MANUAL_PAINEL", verbose_name="Origem")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="EM_ANDAMENTO", db_index=True, verbose_name="Status")
    usuario = models.CharField(max_length=100, default="Sistema", verbose_name="Disparado por")
    iniciado_em = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Iniciado em")
    finalizado_em = models.DateTimeField(blank=True, null=True, verbose_name="Finalizado em")
    duracao_segundos = models.FloatField(default=0.0, verbose_name="Duração (s)")
    registros_criados = models.IntegerField(default=0, verbose_name="Registros Criados")
    registros_atualizados = models.IntegerField(default=0, verbose_name="Registros Atualizados")
    mensagem = models.TextField(blank=True, null=True, verbose_name="Resumo / Detalhes")

    class Meta:
        verbose_name = "Log de Sincronização"
        verbose_name_plural = "Logs de Sincronização"
        ordering = ["-iniciado_em"]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.get_tipo_display()} ({self.iniciado_em:%d/%m/%Y %H:%M})"