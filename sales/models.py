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


class ContaReceber(models.Model):
    STATUS_CHOICES = [
        ("A_VENCER", "A Vencer"),
        ("VENCIDO", "Vencido / Em Atraso"),
        ("RECEBIDO", "Recebido / Quitado"),
        ("CANCELADO", "Cancelado"),
    ]

    empresa = models.CharField(max_length=100, db_index=True, verbose_name="Empresa")
    id_titulo_erp = models.CharField(max_length=50, db_index=True, verbose_name="ID Título ERP (T002_Id)")
    numero_documento = models.CharField(max_length=60, blank=True, null=True, db_index=True, verbose_name="Número NF / Doc")
    numero_duplicata = models.CharField(max_length=60, blank=True, null=True, db_index=True, verbose_name="Número Duplicata")
    parcela = models.CharField(max_length=30, blank=True, null=True, verbose_name="Parcela")

    cliente_id_erp = models.CharField(max_length=30, blank=True, null=True, verbose_name="ID Cliente ERP")
    cliente_nome = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name="Cliente / Razão Social")
    cliente_documento = models.CharField(max_length=30, blank=True, null=True, db_index=True, verbose_name="CNPJ/CPF")
    vendedor_nome = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="Vendedor")

    data_emissao = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Emissão")
    data_vencimento = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Vencimento")
    data_recebimento = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Recebimento")
    data_baixa = models.DateField(blank=True, null=True, verbose_name="Data de Baixa")
    prazo_dias = models.IntegerField(default=0, verbose_name="Prazo (Dias)")
    dias_atraso = models.IntegerField(default=0, verbose_name="Dias de Atraso")

    valor_duplicata = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Original (R$)")
    valor_juros = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Juros (R$)")
    valor_desconto = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Desconto (R$)")
    valor_total = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Total (R$)")
    valor_recebido = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Recebido (R$)")
    valor_saldo = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Saldo a Receber (R$)")
    valor_comissao = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Comissão (R$)")

    portador = models.CharField(max_length=100, blank=True, null=True, verbose_name="Portador / Banco")
    subconta = models.CharField(max_length=150, blank=True, null=True, verbose_name="Subconta Financeira")
    grupo_conta = models.CharField(max_length=150, blank=True, null=True, verbose_name="Grupo de Conta")
    nosso_numero = models.CharField(max_length=60, blank=True, null=True, verbose_name="Nosso Número (Boleto)")
    observacao = models.TextField(blank=True, null=True, verbose_name="Observação")

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="A_VENCER", db_index=True, verbose_name="Status")
    cancelada = models.BooleanField(default=False, db_index=True, verbose_name="Cancelada")
    dados_brutos = models.JSONField(blank=True, null=True, verbose_name="Payload Bruto")
    data_sincronizacao = models.DateTimeField(auto_now=True, verbose_name="Última Sincronização")

    class Meta:
        verbose_name = "Conta a Receber"
        verbose_name_plural = "Contas a Receber"
        unique_together = ("empresa", "id_titulo_erp")
        ordering = ["-data_vencimento", "-id_titulo_erp"]
        indexes = [
            models.Index(fields=["status", "data_vencimento"]),
            models.Index(fields=["empresa", "data_emissao"]),
        ]

    def __str__(self):
        return f"CR {self.numero_duplicata or self.id_titulo_erp} ({self.empresa}) - {self.cliente_nome}: R$ {self.valor_total:,.2f}"


class ContaPagar(models.Model):
    STATUS_CHOICES = [
        ("A_VENCER", "A Vencer"),
        ("VENCIDO", "Vencido / Em Atraso"),
        ("PAGO", "Pago / Quitado"),
        ("CANCELADO", "Cancelado"),
    ]

    empresa = models.CharField(max_length=100, db_index=True, verbose_name="Empresa")
    id_titulo_erp = models.CharField(max_length=50, db_index=True, verbose_name="ID Título ERP (T015_Id)")
    numero_documento = models.CharField(max_length=60, blank=True, null=True, db_index=True, verbose_name="Número Documento")
    numero_duplicata = models.CharField(max_length=60, blank=True, null=True, db_index=True, verbose_name="Número Duplicata")
    parcela = models.CharField(max_length=30, blank=True, null=True, verbose_name="Parcela")

    fornecedor_id_erp = models.CharField(max_length=30, blank=True, null=True, verbose_name="ID Fornecedor ERP")
    fornecedor_nome = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name="Fornecedor / Credor")
    fornecedor_documento = models.CharField(max_length=30, blank=True, null=True, db_index=True, verbose_name="CNPJ/CPF")

    data_emissao = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Emissão")
    data_vencimento = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Vencimento")
    data_pagamento = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Pagamento")
    prazo_dias = models.IntegerField(default=0, verbose_name="Prazo (Dias)")
    dias_atraso = models.IntegerField(default=0, verbose_name="Dias de Atraso")

    valor_duplicata = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Original (R$)")
    valor_juros = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Juros (R$)")
    valor_desconto = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Desconto (R$)")
    valor_total = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Total (R$)")
    valor_pago = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Pago (R$)")
    valor_saldo = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Saldo a Pagar (R$)")

    centro_custo = models.CharField(max_length=150, blank=True, null=True, db_index=True, verbose_name="Centro de Custo")
    subconta = models.CharField(max_length=150, blank=True, null=True, verbose_name="Subconta Financeira")
    grupo_conta = models.CharField(max_length=150, blank=True, null=True, verbose_name="Grupo de Conta")
    portador = models.CharField(max_length=100, blank=True, null=True, verbose_name="Portador / Forma Pagto")
    codigo_barras = models.CharField(max_length=120, blank=True, null=True, verbose_name="Código de Barras")
    observacao = models.TextField(blank=True, null=True, verbose_name="Observação")

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="A_VENCER", db_index=True, verbose_name="Status")
    cancelada = models.BooleanField(default=False, db_index=True, verbose_name="Cancelada")
    dados_brutos = models.JSONField(blank=True, null=True, verbose_name="Payload Bruto")
    data_sincronizacao = models.DateTimeField(auto_now=True, verbose_name="Última Sincronização")

    class Meta:
        verbose_name = "Conta a Pagar"
        verbose_name_plural = "Contas a Pagar"
        unique_together = ("empresa", "id_titulo_erp")
        ordering = ["-data_vencimento", "-id_titulo_erp"]
        indexes = [
            models.Index(fields=["status", "data_vencimento"]),
            models.Index(fields=["empresa", "data_emissao"]),
        ]

    def __str__(self):
        return f"CP {self.numero_duplicata or self.id_titulo_erp} ({self.empresa}) - {self.fornecedor_nome}: R$ {self.valor_total:,.2f}"


class Orcamento(models.Model):
    STATUS_CHOICES = [
        ("PENDENTE", "Pendente / Em Aberto"),
        ("FINALIZADO", "Finalizado / Ganho"),
        ("PERDIDO", "Perdido"),
        ("CANCELADO", "Cancelado"),
    ]

    empresa = models.CharField(max_length=100, db_index=True, verbose_name="Empresa")
    numero_orcamento = models.CharField(max_length=50, db_index=True, verbose_name="Número do Orçamento (T003_Id)")
    data_emissao = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Emissão")
    hora_inclusao = models.CharField(max_length=20, blank=True, null=True, verbose_name="Hora de Inclusão")

    cliente_id_erp = models.CharField(max_length=30, blank=True, null=True, verbose_name="ID Cliente ERP")
    cliente_nome = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name="Cliente / Fantasia")
    contato = models.CharField(max_length=150, blank=True, null=True, verbose_name="Contato")
    cidade = models.CharField(max_length=100, blank=True, null=True, verbose_name="Cidade")
    uf = models.CharField(max_length=10, blank=True, null=True, verbose_name="UF")

    vendedor_nome = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="Vendedor Interno")
    vendedor_externo = models.CharField(max_length=100, blank=True, null=True, verbose_name="Vendedor Externo")
    cfop = models.CharField(max_length=20, blank=True, null=True, verbose_name="CFOP")
    marca = models.CharField(max_length=120, blank=True, null=True, verbose_name="Marca Principal")

    valor_produtos = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Produtos (R$)")
    valor_desconto = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Desconto (R$)")
    valor_frete = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Frete (R$)")
    valor_total = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Total (R$)")
    valor_pendente = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Pendente (R$)")
    valor_custo = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Custo Estimado (R$)")
    valor_comissao = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Comissão Estimada (R$)")
    percentual_margem = models.DecimalField(max_digits=14, decimal_places=2, default=0.00, verbose_name="Margem (%)")
    ipv = models.DecimalField(max_digits=14, decimal_places=4, default=0.0000, verbose_name="IPV")

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="PENDENTE", db_index=True, verbose_name="Status")
    flag_status = models.CharField(max_length=10, blank=True, null=True, verbose_name="Flag Status (P/F/C)")
    flag_perdido = models.CharField(max_length=10, blank=True, null=True, verbose_name="Flag Perdido (N/S/F)")
    motivo_perda = models.TextField(blank=True, null=True, verbose_name="Observação Orçamento Perdido")
    pedido_gerado = models.CharField(max_length=100, blank=True, null=True, verbose_name="Pedido Gerado")
    numero_nota = models.CharField(max_length=50, blank=True, null=True, verbose_name="Nota Fiscal Vinculada")
    observacao = models.TextField(blank=True, null=True, verbose_name="Observação")

    dados_brutos = models.JSONField(blank=True, null=True, verbose_name="Payload Bruto")
    data_sincronizacao = models.DateTimeField(auto_now=True, verbose_name="Última Sincronização")

    class Meta:
        verbose_name = "Orçamento"
        verbose_name_plural = "Orçamentos"
        unique_together = ("empresa", "numero_orcamento")
        ordering = ["-data_emissao", "-numero_orcamento"]
        indexes = [
            models.Index(fields=["status", "data_emissao"]),
            models.Index(fields=["vendedor_nome", "data_emissao"]),
        ]

    def __str__(self):
        return f"Orç. {self.numero_orcamento} ({self.empresa}) - {self.cliente_nome}: R$ {self.valor_total:,.2f}"


class ItemOrcamento(models.Model):
    id_item_erp = models.CharField(max_length=60, unique=True, db_index=True, verbose_name="ID Item (Empresa-T004_Id)")
    empresa = models.CharField(max_length=100, db_index=True, verbose_name="Empresa")
    numero_orcamento = models.CharField(max_length=50, db_index=True, verbose_name="Número do Orçamento (T003_Id)")
    orcamento = models.ForeignKey(
        Orcamento,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="itens",
        verbose_name="Orçamento Vinculado",
    )
    codigo_produto = models.CharField(max_length=50, db_index=True, verbose_name="Código do Produto (SKU)")
    descricao_produto = models.CharField(max_length=255, verbose_name="Descrição do Produto")
    marca = models.CharField(max_length=100, blank=True, null=True, verbose_name="Marca")
    unidade = models.CharField(max_length=20, blank=True, null=True, verbose_name="Unidade")
    quantidade = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Quantidade")
    valor_unitario = models.DecimalField(max_digits=12, decimal_places=4, default=0.00, verbose_name="Valor Unitário (R$)")
    valor_total = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name="Valor Total (R$)")
    data_emissao = models.DateField(blank=True, null=True, db_index=True, verbose_name="Data de Emissão")
    cliente_nome = models.CharField(max_length=255, blank=True, null=True, verbose_name="Cliente")
    vendedor_nome = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="Vendedor")
    dados_brutos = models.JSONField(blank=True, null=True, verbose_name="Payload Bruto")
    data_sincronizacao = models.DateTimeField(auto_now=True, verbose_name="Última Sincronização")

    class Meta:
        verbose_name = "Item de Orçamento"
        verbose_name_plural = "Itens de Orçamento"
        ordering = ["-data_emissao", "-numero_orcamento"]
        indexes = [
            models.Index(fields=["data_emissao", "codigo_produto"]),
            models.Index(fields=["numero_orcamento", "empresa"]),
        ]

    def __str__(self):
        return f"Item Orç. {self.numero_orcamento} ({self.empresa}): {self.codigo_produto} - {self.descricao_produto} ({self.quantidade} un)"


class LogSincronizacao(models.Model):
    TIPO_CHOICES = [
        ("NOTAS_HARDNESS", "Notas Fiscais (Hardness)"),
        ("ESTOQUE_HARDNESS", "Estoque & Itens (Hardness)"),
        ("FINANCEIRO_HARDNESS", "Financeiro CR/CP (Hardness)"),
        ("ORCAMENTOS_HARDNESS", "Orçamentos CRM (Hardness)"),
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


class ConfiguracaoFinanceira(models.Model):
    """
    Armazena o saldo bancário manual informado pela gestão (sem integração bancária),
    a reserva mínima operacional (padrão R$ 30.000,00) e parâmetros de alertas de custos.
    """
    empresa = models.CharField(
        max_length=100,
        unique=True,
        default="TODAS",
        db_index=True,
        verbose_name="Empresa / Escopo",
    )
    saldo_bancario_atual = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0.00,
        verbose_name="Saldo Bancário Atual Informado (R$)",
    )
    reserva_minima = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=30000.00,
        verbose_name="Reserva Mínima Operacional (R$)",
    )
    alerta_aumento_pct = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=10.00,
        verbose_name="Alerta Aumento Relevante (%)",
    )
    alerta_aumento_valor = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=500.00,
        verbose_name="Alerta Aumento Relevante Mínimo (R$)",
    )
    usuario_atualizacao = models.CharField(
        max_length=100,
        default="Sistema",
        verbose_name="Usuário Responsável pela Atualização",
    )
    data_atualizacao = models.DateTimeField(
        auto_now=True,
        verbose_name="Data e Hora da Atualização",
    )

    class Meta:
        verbose_name = "Configuração Financeira / Saldo Bancário"
        verbose_name_plural = "Configurações Financeiras / Saldos Bancários"
        ordering = ["empresa"]

    def __str__(self):
        return f"Saldo ({self.empresa}): R$ {self.saldo_bancario_atual:,.2f} (Reserva: R$ {self.reserva_minima:,.2f})"


class ClassificacaoCusto(models.Model):
    """
    Mapeamento persistente e customizável de cada Conta de Despesa do ERP:
    Conta -> Categoria -> Subcategoria -> Tipo de Custo (A a E) + Recorrente.
    """
    TIPO_CUSTO_CHOICES = [
        ("CMV", "A. Custo de Mercadorias / CMV"),
        ("FIXO", "B. Custos Fixos"),
        ("VARIAVEL", "C. Custos Variáveis"),
        ("FINANCEIRO", "D. Despesas Financeiras"),
        ("EXTRAORDINARIO", "E. Despesas Extraordinárias"),
    ]

    conta_chave = models.CharField(
        max_length=200,
        unique=True,
        db_index=True,
        verbose_name="Conta (Subconta / Grupo ERP)",
    )
    grupo_conta_erp = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Grupo de Conta ERP",
    )
    centro_custo_erp = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Centro de Custo ERP",
    )
    categoria = models.CharField(
        max_length=100,
        db_index=True,
        verbose_name="Categoria Gerencial",
    )
    subcategoria = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Subcategoria",
    )
    tipo_custo = models.CharField(
        max_length=30,
        choices=TIPO_CUSTO_CHOICES,
        default="FIXO",
        db_index=True,
        verbose_name="Tipo de Custo",
    )
    recorrente = models.BooleanField(
        default=False,
        verbose_name="Custo Fixo Recorrente",
    )
    editado_manualmente = models.BooleanField(
        default=False,
        verbose_name="Editado pela Gestão",
    )
    atualizado_por = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        default="Sistema",
        verbose_name="Atualizado por",
    )
    data_atualizacao = models.DateTimeField(
        auto_now=True,
        verbose_name="Última Atualização",
    )

    class Meta:
        verbose_name = "Classificação de Custo"
        verbose_name_plural = "Classificações de Custos"
        ordering = ["tipo_custo", "categoria", "conta_chave"]

    def __str__(self):
        return f"{self.conta_chave} -> {self.categoria} / {self.subcategoria or '-'} ({self.get_tipo_custo_display()})"

