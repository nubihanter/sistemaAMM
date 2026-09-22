from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "role", "is_staff")
    list_filter = ("role", "is_staff", "is_active")
    search_fields = ("username", "first_name", "last_name", "email")

    # Adiciona o campo de role nos fieldsets do Django Admin
    fieldsets = UserAdmin.fieldsets + (
        ("Controle de Acesso", {"fields": ("role",)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Controle de Acesso", {"fields": ("role",)}),
    )