import urllib.parse
from app import app

class VercelPathFixMiddleware:
    """
    Middleware متقدم وشامل لمعالجة مسارات Vercel Serverless Functions:
    - يستخرج المسار الفعلي المطلوب بدقة سواء أتى عبر بارامتر __vercel_path أو من خلال HTTP_X_MATCHED_PATH أو PATH_INFO.
    - يتعامل مع الصفحة الرئيسية (/) والمسارات الفرعية (/chat/stream) بكفاءة ويمنع أخطاء 404 و 405 تماماً.
    - ينظف QUERY_STRING من أي بارامترات داخلية خاصة بإعادة التوجيه.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        target_path = None

        # 1. فحص بارامتر __vercel_path في QUERY_STRING
        query_string = environ.get("QUERY_STRING", "")
        if "__vercel_path" in query_string:
            qs = urllib.parse.parse_qs(query_string, keep_blank_values=True)
            if "__vercel_path" in qs:
                raw_val = qs["__vercel_path"][0] if qs["__vercel_path"] else ""
                val = urllib.parse.unquote(raw_val).strip().lstrip("/")
                target_path = "/" + val if val else "/"
                # تنظيف QUERY_STRING
                remaining = [p for p in query_string.split("&") if not p.startswith("__vercel_path=")]
                environ["QUERY_STRING"] = "&".join(remaining)

        # 2. فحص الهيدر HTTP_X_MATCHED_PATH إن لم يُحدد المسار بعد
        if not target_path:
            matched = environ.get("HTTP_X_MATCHED_PATH", "")
            if matched:
                if "__vercel_path=" in matched:
                    raw_val = matched.split("__vercel_path=", 1)[1].split("&", 1)[0]
                    val = urllib.parse.unquote(raw_val).strip().lstrip("/")
                    target_path = "/" + val if val else "/"
                else:
                    for prefix in ["/api/index.py", "/api/index", "/api"]:
                        if matched.startswith(prefix):
                            matched = matched[len(prefix):]
                            break
                    target_path = matched if matched.startswith("/") and len(matched) > 1 else "/"

        # 3. الاعتماد على PATH_INFO كخيار احتياطي
        if not target_path:
            path = environ.get("PATH_INFO", "")
            if "__vercel_path=" in path:
                raw_val = path.split("__vercel_path=", 1)[1].split("&", 1)[0]
                val = urllib.parse.unquote(raw_val).strip().lstrip("/")
                target_path = "/" + val if val else "/"
            else:
                for prefix in ["/api/index.py", "/api/index", "/api"]:
                    if path.startswith(prefix):
                        path = path[len(prefix):]
                        break
                target_path = path if path.startswith("/") and len(path) > 1 else "/"

        # تأكيد ضبط المسار النهائي بدقة
        environ["PATH_INFO"] = target_path or "/"
        return self.wsgi_app(environ, start_response)

app.wsgi_app = VercelPathFixMiddleware(app.wsgi_app)
