from django.urls import path
from .views import dashboard_vendas

urlpatterns = [
    path('dashboard/', dashboard_vendas, name='dashboard_vendas'),
]