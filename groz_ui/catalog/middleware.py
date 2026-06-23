from django.contrib.auth import logout
from django.shortcuts import redirect

EXEMPT_PATHS = {'/admin-panel/login/', '/admin-panel/logout/', '/admin-panel/api/ping/'}

NO_CACHE = {
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
    'Expires': '0',
}


def _redirect_to_login(path):
    r = redirect(f'/admin-panel/login/?next={path}')
    for k, v in NO_CACHE.items():
        r[k] = v
    return r


class AdminNoCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/admin-panel/') and request.path not in EXEMPT_PATHS:
            # Hard server-side check — runs on EVERY request including bfcache misses
            if not request.user.is_authenticated or not request.user.is_staff:
                return _redirect_to_login(request.path)

            token = request.session.get('admin_access_token', '')
            if not token:
                logout(request)
                return _redirect_to_login(request.path)

            # Rotate token on every request — old cached pages will have stale token
            # bfcache pages restored from memory will make a fresh fetch and get 403
            import secrets
            request.session['admin_access_token'] = secrets.token_hex(32)
            request.session.modified = True

        response = self.get_response(request)

        if request.path.startswith('/admin-panel/'):
            for k, v in NO_CACHE.items():
                response[k] = v

        return response
