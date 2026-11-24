from app import app

class VercelPathFixMiddleware:
    """
    Middleware لإصلاح مسارات Vercel:
    يسترجع المسار الأصلي الحقيقي من هيدر HTTP_X_MATCHED_PATH الذي يرسله Vercel مع الطلب،
    حتى يعرف فلاسك المسار الحقيقي المطلوب (/chat/stream) بدلاً من تحويله إلى /.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        matched_path = environ.get("HTTP_X_MATCHED_PATH")
        if matched_path:
            # تنظيف أي بادئة لـ /api إذا وجدت
            for prefix in ["/api/index.py", "/api/index"]:
                if matched_path.startswith(prefix):
                    matched_path = matched_path[len(prefix):]
                    break
            if not matched_path.startswith("/"):
                matched_path = "/" + matched_path
            environ["PATH_INFO"] = matched_path
        else:
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
