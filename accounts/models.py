# accounts/models.py
from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    class Role(models.TextChoices):
        ADMINISTRADOR = "ADMINISTRADOR", "Administrador"
        SUPERVISOR = "SUPERVISOR", "Supervisor"
        VENDEDOR = "VENDEDOR", "Vendedor"
        ALMOXARIFADO = "ALMOXARIFADO", "Almoxarifado"

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.VENDEDOR,
        verbose_name="Perfil de Acesso"
    )
    
    # Campo para casar o login do Django com o nome que vem do Hardness (ex: "GABRIELLI")
    nome_vendedor_erp = models.CharField(
        max_length=100, 
        blank=True, 
        null=True, 
        verbose_name="Nome do Vendedor no Hardness/ERP",
        help_text="Obrigatório para usuários com perfil Vendedor. Ex: GABRIELLI"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == self.Role.ADMINISTRADOR or self.is_superuser

    @property
    def is_supervisor(self) -> bool:
        return self.role in [self.Role.SUPERVISOR, self.Role.ADMINISTRADOR] or self.is_superuser

    @property
    def is_vendedor(self) -> bool:
        return self.role == self.Role.VENDEDOR

    @property
    def is_almoxarifado(self) -> bool:
        return self.role == self.Role.ALMOXARIFADO