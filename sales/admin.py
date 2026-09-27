from django.contrib import admin
from .models import (
    ContaPagar,
    ContaReceber,
    LogSincronizacao,
    MetaVendedor,
    NotaFiscal,
    Orcamento,
    Vendedor,
)


@admin.register(NotaFiscal)
class NotaFiscalAdmin(admin.ModelAdmin):
    list_display = ("numero_nota", "empresa", "data_emissao", "cliente_nome", "vendedor_nome", "valor_total", "status")
    list_filter = ("empresa", "status", "data_emissao")
    search_fields = ("numero_nota", "cliente_nome", "cliente_documento", "vendedor_nome")
    date_hierarchy = "data_emissao"


@admin.register(ContaReceber)
class ContaReceberAdmin(admin.ModelAdmin):
    list_display = (
        "numero_duplicata",
        "empresa",
        "cliente_nome",
        "data_emissao",
        "data_vencimento",
        "data_recebimento",
        "valor_total",
        "valor_saldo",
        "status",
    )
    list_filter = ("empresa", "status", "cancelada", "data_vencimento")
    search_fields = ("numero_duplicata", "numero_documento", "cliente_nome", "cliente_documento", "nosso_numero")
    date_hierarchy = "data_vencimento"


@admin.register(ContaPagar)
class ContaPagarAdmin(admin.ModelAdmin):
    list_display = (
        "numero_duplicata",
        "empresa",
        "fornecedor_nome",
        "centro_custo",
        "data_emissao",
        "data_vencimento",
        "data_pagamento",
        "valor_total",
        "valor_saldo",
        "status",
    )
    list_filter = ("empresa", "status", "cancelada", "centro_custo", "data_vencimento")
    search_fields = ("numero_duplicata", "numero_documento", "fornecedor_nome", "fornecedor_documento")
    date_hierarchy = "data_vencimento"


@admin.register(Orcamento)
class OrcamentoAdmin(admin.ModelAdmin):
    list_display = (
        "numero_orcamento",
        "empresa",
        "data_emissao",
        "cliente_nome",
        "vendedor_nome",
        "valor_total",
        "percentual_margem",
        "status",
        "pedido_gerado",
    )
    list_filter = ("empresa", "status", "data_emissao", "vendedor_nome")
    search_fields = ("numero_orcamento", "cliente_nome", "vendedor_nome", "pedido_gerado", "numero_nota")
    date_hierarchy = "data_emissao"


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


@admin.register(LogSincronizacao)
class LogSincronizacaoAdmin(admin.ModelAdmin):
    list_display = (
        "iniciado_em",
        "tipo",
        "origem",
        "status",
        "usuario",
        "duracao_segundos",
        "registros_criados",
        "registros_atualizados",
    )
    list_filter = ("tipo", "origem", "status")
    search_fields = ("usuario", "mensagem")
    readonly_fields = (
        "tipo",
        "origem",
        "status",
        "usuario",
        "iniciado_em",
        "finalizado_em",
        "duracao_segundos",
        "registros_criados",
        "registros_atualizados",
        "mensagem",
    )