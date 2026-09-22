from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
from decimal import Decimal


class Categoria(models.Model):
    nome = models.CharField(max_length=100, unique=True, verbose_name="Nome da Categoria")
    descricao = models.TextField(blank=True, null=True, verbose_name="Descrição")

    class Meta:
        verbose_name = "Categoria"
        verbose_name_plural = "Categorias"
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class CertificadoAprovacao(models.Model):
    STATUS_CA_CHOICES = [
        ("VALIDO", "Válido"),
        ("VENCIDO", "Vencido"),
        ("CANCELADO", "Cancelado"),
        ("SUSPENSO", "Suspenso"),
    ]

    numero_ca = models.CharField(
        max_length=20, 
        unique=True, 
        db_index=True, 
        verbose_name="Número do CA"
    )
    data_validade = models.DateField(verbose_name="Data de Validade")
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CA_CHOICES, 
        default="VALIDO", 
        verbose_name="Status do CA"
    )
    fabricante = models.CharField(max_length=255, verbose_name="Fabricante / Importador")
    descricao_equipamento = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="Descrição do Equipamento (MTE)"
    )
    normas_atendidas = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="Normas Técnicas Atendidas"
    )
    laudo_pdf = models.FileField(
        upload_to="laudos_ca/", 
        blank=True, 
        null=True, 
        verbose_name="Arquivo do Laudo / Ficha Técnica"
    )
    ultima_consulta_api = models.DateTimeField(
        blank=True, 
        null=True, 
        verbose_name="Última Sincronização via API"
    )

    class Meta:
        verbose_name = "Certificado de Aprovação (CA)"
        verbose_name_plural = "Certificados de Aprovação (CA)"
        ordering = ["numero_ca"]

    def __str__(self):
        return f"CA {self.numero_ca} - {self.fabricante}"

    @property
    def esta_vencido(self) -> bool:
        """Verifica dinamicamente se a data de validade já expirou."""
        if not self.data_validade:
            return False
        return self.data_validade < timezone.now().date()

    @property
    def dias_para_vencer(self) -> int:
        """Retorna a contagem de dias restantes até o vencimento."""
        if not self.data_validade:
            return 0
        delta = self.data_validade - timezone.now().date()
        return delta.days


class ProdutoEPI(models.Model):
    UNIDADE_MEDIDA_CHOICES = [
        ("UN", "Unidade"),
        ("PAR", "Par"),
        ("CJ", "Conjunto"),
        ("CX", "Caixa"),
        ("PCT", "Pacote"),
    ]

    sku = models.CharField(max_length=50, unique=True, verbose_name="SKU / Código Interno")
    nome = models.CharField(max_length=200, verbose_name="Nome do Produto")
    categoria = models.ForeignKey(
        Categoria, 
        on_delete=models.PROTECT, 
        related_name="produtos", 
        verbose_name="Categoria"
    )
    ca = models.ForeignKey(
        CertificadoAprovacao, 
        on_delete=models.PROTECT, 
        related_name="produtos", 
        verbose_name="Certificado de Aprovação (CA)"
    )
    tamanho_variacao = models.CharField(
        max_length=50, 
        blank=True, 
        null=True, 
        verbose_name="Tamanho / Variação (ex: G, 42, Único)"
    )
    unidade_medida = models.CharField(
        max_length=5, 
        choices=UNIDADE_MEDIDA_CHOICES, 
        default="UN", 
        verbose_name="Unidade de Medida"
    )
    
    # Preços
    preco_custo = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"), verbose_name="Preço de Custo")
    preco_venda = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"), verbose_name="Preço de Venda")
    
    # Controle de Estoque
    estoque_atual = models.PositiveIntegerField(default=0, verbose_name="Estoque Atual")
    estoque_minimo = models.PositiveIntegerField(default=5, verbose_name="Estoque Mínimo de Segurança")
    
    ativo = models.BooleanField(default=True, verbose_name="Ativo para Venda")
    data_cadastro = models.DateTimeField(auto_now_add=True, verbose_name="Data de Cadastro")
    data_atualizacao = models.DateTimeField(auto_now=True, verbose_name="Última Atualização")

    class Meta:
        verbose_name = "Produto EPI"
        verbose_name_plural = "Produtos EPIs"
        ordering = ["nome"]

    def __str__(self):
        var = f" ({self.tamanho_variacao})" if self.tamanho_variacao else ""
        return f"{self.sku} - {self.nome}{var}"

    @property
    def estoque_critico(self) -> bool:
        """Alerta se o estoque atingiu ou ficou abaixo do nível de segurança."""
        return self.estoque_atual <= self.estoque_minimo

    @property
    def apto_para_venda(self) -> bool:
        """Verifica se está ativo, com saldo e com o CA regularizado."""
        return self.ativo and self.estoque_atual > 0 and not self.ca.esta_vencido


class MovimentacaoEstoque(models.Model):
    TIPO_MOVIMENTO_CHOICES = [
        ("ENTRADA", "Entrada (Compra / Devolução)"),
        ("SAIDA", "Saída (Venda / Descarte)"),
        ("AJUSTE", "Ajuste de Balanço / Inventário"),
    ]

    produto = models.ForeignKey(
        ProdutoEPI, 
        on_delete=models.CASCADE, 
        related_name="movimentacoes", 
        verbose_name="Produto"
    )
    tipo = models.CharField(max_length=10, choices=TIPO_MOVIMENTO_CHOICES, verbose_name="Tipo de Movimento")
    quantidade = models.PositiveIntegerField(verbose_name="Quantidade Movimentada")
    motivo = models.CharField(max_length=255, verbose_name="Motivo / Observação")
    data_hora = models.DateTimeField(default=timezone.now, verbose_name="Data/Hora")

    class Meta:
        verbose_name = "Movimentação de Estoque"
        verbose_name_plural = "Movimentações de Estoque"
        ordering = ["-data_hora"]

    def __str__(self):
        return f"{self.tipo} - {self.produto.sku} ({self.quantidade})"

    def clean(self):
        """Valida se uma saída não vai deixar o saldo negativo."""
        if self.tipo == "SAIDA" and self.produto_id:
            if self.produto.estoque_atual < self.quantidade:
                raise ValidationError("Estoque insuficiente para registrar esta saída.")

    def save(self, *args, **kwargs):
        """Garante a atualização atômica do saldo do produto ao registrar a movimentação."""
        self.full_clean()
        is_new = self._state.adding

        if is_new:
            if self.tipo == "ENTRADA":
                self.produto.estoque_atual += self.quantidade
            elif self.tipo == "SAIDA":
                self.produto.estoque_atual -= self.quantidade
            elif self.tipo == "AJUSTE":
                self.produto.estoque_atual = self.quantidade
            
            self.produto.save(update_fields=["estoque_atual", "data_atualizacao"])

        super().save(*args, **kwargs)