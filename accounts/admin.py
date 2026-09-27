from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "first_name", "role", "nome_vendedor_erp", "is_active", "is_staff")
    list_editable = ("role", "nome_vendedor_erp", "is_active")
    list_filter = ("role", "is_staff", "is_active")
    search_fields = ("username", "first_name", "last_name", "email", "nome_vendedor_erp")

    # Adiciona os campos customizados nos fieldsets do Django Admin
    fieldsets = UserAdmin.fieldsets + (
        ("Controle de Acesso & ERP", {"fields": ("role", "nome_vendedor_erp")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Controle de Acesso & ERP", {"fields": ("role", "nome_vendedor_erp")}),
    )