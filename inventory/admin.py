from datetime import timedelta
from django.contrib import admin
from django.db.models import F
from django.utils import timezone
from django.utils.html import format_html

from .models import Categoria, CertificadoAprovacao, ProdutoEPI, MovimentacaoEstoque


# ==========================================
# FILTROS CUSTOMIZADOS
# ==========================================

class ValidadeCAFilter(admin.SimpleListFilter):
    title = "Situação da Validade do CA"
    parameter_name = "situacao_validade"

    def lookups(self, request, model_admin):
        return [
            ("vencido", "Já Vencido"),
            ("vence_30", "Vence em até 30 dias"),
            ("vence_60", "Vence em até 60 dias"),
            ("valido", "Válido (> 60 dias)"),
        ]

    def queryset(self, request, queryset):
        hoje = timezone.now().date()
        
        # Ajusta o queryset se estiver sendo chamado a partir de ProdutoEPI
        prefixo = "ca__" if hasattr(queryset.model, "ca") else ""

        if self.value() == "vencido":
            return queryset.filter(**{f"{prefixo}data_validade__lt": hoje})
        if self.value() == "vence_30":
            return queryset.filter(
                **{
                    f"{prefixo}data_validade__gte": hoje,
                    f"{prefixo}data_validade__lte": hoje + timedelta(days=30),
                }
            )
        if self.value() == "vence_60":
            return queryset.filter(
                **{
                    f"{prefixo}data_validade__gte": hoje,
                    f"{prefixo}data_validade__lte": hoje + timedelta(days=60),
                }
            )
        if self.value() == "valido":
            return queryset.filter(**{f"{prefixo}data_validade__gt": hoje + timedelta(days=60)})
        return queryset


class EstoqueCriticoFilter(admin.SimpleListFilter):
    title = "Alerta de Estoque"
    parameter_name = "alerta_estoque"

    def lookups(self, request, model_admin):
        return [
            ("zerado", "Estoque Zerado"),
            ("critico", "Abaixo ou Igual ao Mínimo"),
            ("normal", "Estoque Regular"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "zerado":
            return queryset.filter(estoque_atual=0)
        if self.value() == "critico":
            return queryset.filter(estoque_atual__lte=F("estoque_minimo"), estoque_atual__gt=0)
        if self.value() == "normal":
            return queryset.filter(estoque_atual__gt=F("estoque_minimo"))
        return queryset


# ==========================================
# INLINES
# ==========================================

class MovimentacaoEstoqueInline(admin.TabularInline):
    model = MovimentacaoEstoque
    extra = 0
    readonly_fields = ("tipo", "quantidade", "motivo", "data_hora")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        # Obriga o cadastro de movimentação pela seção própria para garantir consistência
        return False


# ==========================================
# MODEL ADMINS
# ==========================================

@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ("nome", "total_produtos", "descricao")
    search_fields = ("nome",)

    def total_produtos(self, obj):
        return obj.produtos.count()
    total_produtos.short_description = "Qtd. Produtos"


@admin.register(CertificadoAprovacao)
class CertificadoAprovacaoAdmin(admin.ModelAdmin):
    list_display = (
        "numero_ca", 
        "fabricante", 
        "data_validade", 
        "badge_validade", 
        "status", 
        "ultima_consulta_api"
    )
    list_filter = (ValidadeCAFilter, "status")
    search_fields = ("numero_ca", "fabricante", "descricao_equipamento")
    date_hierarchy = "data_validade"
    ordering = ["data_validade"]

    readonly_fields = ("ultima_consulta_api",)

    fieldsets = (
        ("Identificação Oficial", {
            "fields": ("numero_ca", "status", "data_validade", "fabricante")
        }),
        ("Detalhes Técnicos do Laudo", {
            "fields": ("descricao_equipamento", "normas_atendidas", "laudo_pdf")
        }),
        ("Metadados de Sincronização", {
            "fields": ("ultima_consulta_api",),
            "classes": ("collapse",)
        }),
    )

    @admin.display(description="Situação do CA")
    def badge_validade(self, obj):
        dias = obj.dias_para_vencer
        if dias < 0:
            return format_html(
                '<span style="background-color: #fee2e2; color: #991b1b; padding: 3px 8px; border-radius: 4px; font-weight: bold;">Vencido há {} dias</span>',
                abs(dias)
            )
        elif dias <= 30:
            return format_html(
                '<span style="background-color: #fef3c7; color: #92400e; padding: 3px 8px; border-radius: 4px; font-weight: bold;">Vence em {} dias</span>',
                dias
            )
        return format_html(
            '<span style="background-color: #dcfce7; color: #166534; padding: 3px 8px; border-radius: 4px; font-weight: bold;">Regular ({} dias)</span>',
            dias
        )


@admin.register(ProdutoEPI)
class ProdutoEPIAdmin(admin.ModelAdmin):
    list_display = (
        "sku", 
        "nome", 
        "tamanho_variacao", 
        "categoria", 
        "badge_ca", 
        "estoque_display", 
        "preco_venda", 
        "badge_apto_venda"
    )
    list_filter = (EstoqueCriticoFilter, ValidadeCAFilter, "categoria", "ativo")
    search_fields = ("sku", "nome", "ca__numero_ca", "ca__fabricante")
    autocomplete_fields = ("ca", "categoria")
    readonly_fields = ("estoque_atual", "data_cadastro", "data_atualizacao")
    inlines = [MovimentacaoEstoqueInline]

    fieldsets = (
        ("Informações Básicas", {
            "fields": ("sku", "nome", "categoria", "tamanho_variacao", "unidade_medida", "ativo")
        }),
        ("Certificação de Segurança", {
            "fields": ("ca",)
        }),
        ("Precificação", {
            "fields": (("preco_custo", "preco_venda"),)
        }),
        ("Controle de Estoque", {
            "description": "Para alterar o estoque atual, registre uma Movimentação de Estoque.",
            "fields": (("estoque_atual", "estoque_minimo"),)
        }),
        ("Datas", {
            "fields": (("data_cadastro", "data_atualizacao"),),
            "classes": ("collapse",)
        }),
    )

    @admin.display(description="CA Vinculado")
    def badge_ca(self, obj):
        ca = obj.ca
        if ca.esta_vencido:
            return format_html('<span style="color: #dc2626; font-weight: bold;">CA {} (Vencido)</span>', ca.numero_ca)
        return f"CA {ca.numero_ca}"

    @admin.display(description="Saldo em Estoque")
    def estoque_display(self, obj):
        if obj.estoque_atual == 0:
            return format_html('<b style="color: #dc2626;">0 {} (Zerado)</b>', obj.unidade_medida)
        elif obj.estoque_critico:
            return format_html('<b style="color: #d97706;">{} {} (Crítico)</b>', obj.estoque_atual, obj.unidade_medida)
        return f"{obj.estoque_atual} {obj.unidade_medida}"

    @admin.display(description="Apto p/ Venda", boolean=True)
    def badge_apto_venda(self, obj):
        return obj.apto_para_venda


@admin.register(MovimentacaoEstoque)
class MovimentacaoEstoqueAdmin(admin.ModelAdmin):
    list_display = ("data_hora", "produto", "badge_tipo", "quantidade", "motivo")
    list_filter = ("tipo", "data_hora")
    search_fields = ("produto__sku", "produto__nome", "motivo")
    autocomplete_fields = ("produto",)
    date_hierarchy = "data_hora"

    @admin.display(description="Tipo")
    def badge_tipo(self, obj):
        cores = {
            "ENTRADA": ("#dcfce7", "#166534"),
            "SAIDA": ("#fee2e2", "#991b1b"),
            "AJUSTE": ("#e0e7ff", "#3730a3"),
        }
        bg, text = cores.get(obj.tipo, ("#f3f4f6", "#1f2937"))
        return format_html(
            '<span style="background-color: {}; color: {}; padding: 3px 8px; border-radius: 4px; font-weight: bold;">{}</span>',
            bg, text, obj.get_tipo_display()
        )