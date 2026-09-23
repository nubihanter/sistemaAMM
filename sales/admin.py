from django.contrib import admin
from .models import NotaFiscal, MetaVendedor

@admin.register(MetaVendedor)
class MetaVendedorAdmin(admin.ModelAdmin):
    list_display = ("vendedor_nome", "mes", "ano", "valor", "titulo_meta", "data_atualizacao")
    list_filter = ("ano", "mes", "vendedor_nome")
    search_fields = ("vendedor_nome", "titulo_meta")