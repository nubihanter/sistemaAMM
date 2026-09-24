from django.contrib import admin
from .models import NotaFiscal, MetaVendedor, Vendedor

@admin.register(MetaVendedor)
class MetaVendedorAdmin(admin.ModelAdmin):
    list_display = ("vendedor_nome", "mes", "ano", "valor", "titulo_meta", "data_atualizacao")
    list_filter = ("ano", "mes", "vendedor_nome")
    search_fields = ("vendedor_nome", "titulo_meta")

@admin.register(Vendedor)
class VendedorAdmin(admin.ModelAdmin):
    list_display = ("nome_hardness", "nome_piperun", "ativo", "ativo_ranking")
    list_editable = ("nome_piperun", "ativo", "ativo_ranking")
    search_fields = ("nome_hardness", "nome_piperun")
    list_filter = ("ativo", "ativo_ranking")