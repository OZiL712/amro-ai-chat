from app import app
from urllib.parse import parse_qs, unquote

class VercelPathFixMiddleware:
    """
    Middleware لإصلاح مسارات Vercel:
    يسترجع المسار المطلوب بدقة عبر بارامتر __vercel_path الذي يتم تمريره في vercel.json
    أو عبر الهيدرز، لتفادي أي التباس في مسارات الـ POST (مثل 405 Method Not Allowed).
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        query_string = environ.get("QUERY_STRING", "")

        # 1. فحص بارامتر __vercel_path القادم من إعادة توجيه vercel.json
        if "__vercel_path=" in query_string:
            qs = parse_qs(query_string)
            if "__vercel_path" in qs and qs["__vercel_path"]:
                target_path = "/" + unquote(qs["__vercel_path"][0]).lstrip("/")
                environ["PATH_INFO"] = target_path
                remaining = [p for p in query_string.split("&") if not p.startswith("__vercel_path=")]
                environ["QUERY_STRING"] = "&".join(remaining)
                return self.wsgi_app(environ, start_response)

        # 2. فحص الهيدر HTTP_X_MATCHED_PATH
        matched = environ.get("HTTP_X_MATCHED_PATH")
        if matched:
            for prefix in ["/api/index.py", "/api/index"]:
                if matched.startswith(prefix):
                    matched = matched[len(prefix):]
                    break
            if not matched.startswith("/"):
                matched = "/" + matched
            environ["PATH_INFO"] = matched
            return self.wsgi_app(environ, start_response)

        # 3. الوضع الافتراضي
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
