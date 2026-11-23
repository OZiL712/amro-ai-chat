from app import app

class VercelPathFixMiddleware:
    """
    Middleware لمعالجة مسارات Vercel Serverless Functions:
    يقوم بتجريد بادئة /api/index.py أو /api من المسار الوارد حتى يتعرف فلاسك على الصفحة الرئيسية / وبقية المسارات فوراً.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        for prefix in ["/api/index.py", "/api/index", "/api"]:
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        if not path or not path.startswith("/"):
            path = "/" + path
        environ["PATH_INFO"] = path
        return self.wsgi_app(environ, start_response)

app.wsgi_app = VercelPathFixMiddleware(app.wsgi_app)
