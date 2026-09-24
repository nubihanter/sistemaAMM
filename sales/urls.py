from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard_vendas, name='dashboard_vendas'),
    path('clientes/', views.analise_clientes_view, name='analise_clientes'),
]