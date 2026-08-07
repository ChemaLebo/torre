class TenantMiddleware:
    """Cuelga request.cliente (tenant) y request.rol según el perfil del usuario.

    Regla dura multi-tenant: un usuario de portal SOLO ve datos de su cliente.
    Toda vista de portal filtra por request.cliente — nunca por parámetros del usuario.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.cliente = None
        request.rol = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            perfil = getattr(user, "perfil", None)
            if perfil is not None:
                request.rol = perfil.rol
                request.cliente = perfil.cliente
        return self.get_response(request)
