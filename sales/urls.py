from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard_vendas, name='dashboard_vendas'),
    path('dashboard/exportar-excel/', views.exportar_vendas_excel_view, name='exportar_vendas_excel'),
    path('orcamentos/', views.dashboard_orcamentos_view, name='dashboard_orcamentos'),
    path('orcamentos/exportar-excel/', views.exportar_orcamentos_excel_view, name='exportar_orcamentos_excel'),
    path('financeiro/', views.dashboard_financeiro_view, name='dashboard_financeiro'),
    path('clientes/', views.analise_clientes_view, name='analise_clientes'),
    path('clientes/exportar-excel/', views.exportar_clientes_excel_view, name='exportar_clientes_excel'),
    path('sincronizacao/', views.painel_sincronizacao_view, name='painel_sincronizacao'),
    path('sincronizacao/status/', views.status_sincronizacao_json_view, name='status_sincronizacao_json'),
    path('sincronizacao/disparar/', views.disparar_sincronizacao_view, name='disparar_sincronizacao'),
]