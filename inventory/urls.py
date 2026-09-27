from django.urls import path
from . import views

urlpatterns = [
    path("dashboard/", views.dashboard_estoque_view, name="dashboard_estoque"),
    path("produtos/", views.gestao_produtos_view, name="gestao_produtos"),
    path("produtos/salvar/", views.salvar_produto_view, name="salvar_produto"),
    path("ca/salvar/", views.salvar_ou_consultar_ca_view, name="salvar_ou_consultar_ca"),
    path("produto/parametros/", views.atualizar_parametros_produto_view, name="atualizar_parametros_produto"),
    path("sincronizar/", views.sincronizar_estoque_manual_view, name="sincronizar_estoque_manual"),
    path("exportar-csv/", views.exportar_compras_csv_view, name="exportar_compras_csv"),
]
