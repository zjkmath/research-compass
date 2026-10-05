class LocalSecurityHeaders:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        request.allow_registration = settings.ALLOW_REGISTRATION
        request.private_site = settings.PRIVATE_SITE
        if settings.PRIVATE_SITE and not request.user.is_authenticated and not request.path.startswith('/accounts/login/'):
            from django.contrib.auth.views import redirect_to_login
            response = redirect_to_login(request.get_full_path())
        else:
            response = self.get_response(request)
        response['X-Robots-Tag']='noindex, nofollow'
        if not request.path.startswith('/admin/'):
            response['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
            if request.GET.get('enhance')=='off':
                response['Content-Security-Policy']=response['Content-Security-Policy'].replace("script-src 'self'","script-src 'none'")
        response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if settings.PRIVATE_SITE or request.user.is_authenticated or request.path.startswith(('/workspace/', '/accounts/', '/admin/', '/updates/')):
            response['Cache-Control'] = 'private, no-store'
        return response
