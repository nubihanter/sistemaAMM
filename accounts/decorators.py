from django.core.exceptions import PermissionDenied
from functools import wraps


def roles_required(*allowed_roles):
    """
    Decorator para restringir views a determinados perfis de acesso.
    Exemplo de uso: @roles_required('ADMINISTRADOR', 'SUPERVISOR')
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login
                return redirect_to_login(request.get_full_path())
            
            if request.user.role in allowed_roles or request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            
            raise PermissionDenied("Você não tem autorização para acessar esta página.")
        return _wrapped_view
    return decorator