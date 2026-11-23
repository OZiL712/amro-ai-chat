import os
import re
import json
import time
import html
import threading
from collections import defaultdict
from flask import Flask, jsonify, render_template_string, request, Response, stream_with_context
import requests

# تحميل متغيرات البيئة من ملف .env محلياً إن كانت المكتبة متوفرة
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# ==========================================
# 1. إعدادات الأمان ومعدل الطلبات (Rate Limiting)
# ==========================================
class ThreadSafeRateLimiter:
    """
    نظام تحديد معدل الطلبات (Rate Limiter) متقدم وخفيف ومبني في الذاكرة:
    - يحدد عدد الطلبات لكل عنوان IP خلال نافذة زمنية مدتها دقيقة.
    - يحتوي على حماية من الطلبات المتتالية السريعة (Burst Limit).
    - آمن لتعدد الخيوط (Thread-Safe).
    """
    def __init__(self, requests_per_minute=25, burst_limit=6, burst_window=10):
        self.rpm = requests_per_minute
        self.burst_limit = burst_limit
        self.burst_window = burst_window
        self.requests = defaultdict(list)
        self.lock = threading.Lock()

    def is_allowed(self, ip: str):
        now = time.time()
        with self.lock:
            # تنظيف الطلبات الأقدم من 60 ثانية
            self.requests[ip] = [t for t in self.requests[ip] if now - t < 60]

            # فحص تدفق الطلبات السريع (Burst)
            burst_reqs = [t for t in self.requests[ip] if now - t < self.burst_window]
            if len(burst_reqs) >= self.burst_limit:
                retry_after = int(self.burst_window - (now - burst_reqs[0])) + 1
                return False, max(1, retry_after)

            # فحص إجمالي الطلبات في الدقيقة
            if len(self.requests[ip]) >= self.rpm:
                retry_after = int(60 - (now - self.requests[ip][0])) + 1
                return False, max(1, retry_after)

            self.requests[ip].append(now)
            return True, 0

rate_limiter = ThreadSafeRateLimiter(requests_per_minute=30, burst_limit=8, burst_window=10)

def get_client_ip():
    """استخراج عنوان IP الحقيقي حتى مع وجود خوادم وسيطة (Proxies / CDN)"""
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    if request.headers.get("X-Real-IP"):
        return request.headers.get("X-Real-IP").strip()
    return request.remote_addr or "127.0.0.1"

def get_api_key():
    """الحصول على مفتاح الـ API والتحقق من سلامته دون تسريبه"""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    invalid_keys = ["", "ضع_مفتاح_Gemini_API_هنا", "your_new_api_key_here"]
    if not key or key in invalid_keys:
        return None
    return key

# ==========================================
# 2. نماذج الذكاء الاصطناعي وتعليمات النظام
# ==========================================
# قائمة بنماذج Gemini النشطة والمستقرة، مرتبة حسب الأولوية والسرعة
PRIMARY_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.8-flash"
]

SYSTEM_INSTRUCTION_TEXT = (
    "أنت Amro AI ✨، مساعد ذكاء اصطناعي عربي فائق الذكاء واللباقة، صُممت وطُوّرت بعناية على يد المطور عمرو الشرافي (Amro Alshurafy). "
    "أسلوبك ودود، مبدع، دقيق، وسريع الإجابة. "
    "نظّم إجاباتك دوماً بتنسيق Markdown احترافي: استخدم العناوين الواضحة، القوائم النقطية، الجداول المقارنة عند الحاجة، والرموز التعبيرية المناسبة (🚀💡✨📌💻🎯) لجعل القراءة ممتعة وسلسة. "
    "عند كتابة الأكواد البرمجية، اكتب أكواداً نظيفة وشاملة ومكتوبة داخل كتل كود مناسبة مع تحديد لغة البرمجة دائماً (مثل ```python أو ```dart أو ```javascript). "
    "إذا سُئلت عن هويتك أو من طورك، أجب بفخر واعتزاز أنك Amro AI الذي طوّره المهندس عمرو الشرافي (Amro Alshurafy)؛ "
    "مطور برمجيات وتطبيقات وويب وحلول ذكاء اصطناعي (Flutter • React • Python • AI Solutions)، وطالب تقنية معلومات في جامعة شبوة، ورابط معرض أعماله وسيرته الذاتية هو: https://amrocv.vercel.app ."
)

# ==========================================
# 3. التحقق من المدخلات وتطهيرها (Sanitization)
# ==========================================
def validate_and_sanitize_payload(data):
    """التحقق من سلامة البيانات المرسلة وتطهيرها لحماية الخادم وقاعدة البيانات"""
    if not isinstance(data, dict):
        return None, "صيغة الطلب غير صالحة، يجب إرسال كائن JSON."

    question = data.get("question", "")
    if not isinstance(question, str):
        return None, "نص السؤال غير صالح."

    question = question.strip()
    if not question:
        return None, "يرجى كتابة سؤال قبل الإرسال."

    # تقييد حجم الإدخال لحماية الخادم من هجمات حجب الخدمة (DoS)
    if len(question) > 5000:
        return None, "نص السؤال طويل جداً (الحد الأقصى 5000 حرف)."

    raw_history = data.get("history", [])
    if not isinstance(raw_history, list):
        raw_history = []

    clean_history = []
    # الاكتفاء بآخر 20 رسالة للحفاظ على توازن التوكن وسرعة الاستجابة
    for msg in raw_history[-20:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "model"):
            continue
        parts = msg.get("parts")
        if not isinstance(parts, list) or not parts:
            continue
        first_part = parts[0]
        if not isinstance(first_part, dict):
            continue
        text = first_part.get("text", "")
        if not isinstance(text, str) or not text.strip():
            continue
        # تقييد طول الرسائل السابقة لمنع تضخم السياق
        clean_history.append({
            "role": role,
            "parts": [{"text": text[:4000]}]
        })

    return (question, clean_history), None

# ==========================================
# 4. الواجهة الأمامية المتقدمة (Responsive UI)
# ==========================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Amro AI CHAT ✨ | المساعد الذكي</title>
  
  <!-- خط Cairo الأنيق -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800;900&display=swap" rel="stylesheet">
  
  <!-- مكتبة تلوين الأكواد البرمجية Highlight.js -->
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>

  <!-- مكتبة تحويل الـ Markdown (Marked.js) ومكتبة التطهير ضد XSS (DOMPurify) -->
  <script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>

  <style>
    :root {
      --primary: #1d4ed8;
      --primary-hover: #1e40af;
      --primary-light: #eff6ff;
      --primary-border: #bfdbfe;
      --accent: #0ea5e9;
      --bg-main: #f1f5f9;
      --bg-card: #ffffff;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --border-color: #e2e8f0;
      --bot-msg-bg: #ffffff;
      --user-msg-bg: #2563eb;
      --user-msg-text: #ffffff;
      --sidebar-width: 310px;
      --shadow-sm: 0 1px 3px rgba(0,0,0,0.06);
      --shadow-md: 0 4px 14px rgba(0,0,0,0.08);
      --radius-lg: 16px;
      --radius-md: 12px;
      --radius-sm: 8px;
    }

    * {
      box-sizing: border-box;
      font-family: 'Cairo', -apple-system, BlinkMacSystemFont, sans-serif;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }

    body {
      background: var(--bg-main);
      color: var(--text-main);
      height: 100vh;
      height: 100dvh;
      display: flex;
      overflow: hidden;
    }

    /* ============================================================
       الشريط الجانبي: دفتر الملاحظات المطور (Notes Sidebar)
    ============================================================ */
    .sidebar {
      width: var(--sidebar-width);
      background: var(--bg-card);
      border-left: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      height: 100%;
      z-index: 100;
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      box-shadow: -2px 0 10px rgba(0,0,0,0.03);
    }

    .sidebar-header {
      padding: 16px 18px;
      background: #f8fafc;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .sidebar-header h2 {
      font-size: 16px;
      font-weight: 800;
      color: #0f172a;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .sidebar-header h2 .badge {
      background: #e2e8f0;
      color: #475569;
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 12px;
      font-weight: 700;
    }

    .close-sidebar-btn {
      display: none;
      background: none;
      border: none;
      font-size: 20px;
      color: var(--text-muted);
      cursor: pointer;
      padding: 4px;
      border-radius: 6px;
      transition: 0.2s;
    }
    .close-sidebar-btn:hover { background: #e2e8f0; color: #0f172a; }

    .sidebar-actions {
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      border-bottom: 1px solid var(--border-color);
      background: #fff;
    }

    .new-chat-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      width: 100%;
      padding: 10px 14px;
      background: linear-gradient(135deg, #1d4ed8, #2563eb);
      color: #ffffff;
      border: none;
      border-radius: var(--radius-md);
      font-size: 13.5px;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.2s ease;
      box-shadow: 0 2px 6px rgba(37,99,235,0.25);
    }
    .new-chat-btn:hover {
      background: linear-gradient(135deg, #1e40af, #1d4ed8);
      transform: translateY(-1px);
      box-shadow: 0 4px 10px rgba(37,99,235,0.3);
    }

    .search-box {
      position: relative;
      width: 100%;
    }
    .search-box input {
      width: 100%;
      padding: 8px 34px 8px 12px;
      font-size: 13px;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-sm);
      background: #f8fafc;
      color: var(--text-main);
      outline: none;
      transition: border-color 0.2s;
    }
    .search-box input:focus {
      border-color: var(--primary);
      background: #ffffff;
    }
    .search-box .search-icon {
      position: absolute;
      right: 10px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 13px;
      color: var(--text-muted);
      pointer-events: none;
    }

    .notes-list {
      flex: 1;
      overflow-y: auto;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .empty-notes {
      text-align: center;
      color: var(--text-muted);
      font-size: 13px;
      margin: 40px 10px;
      line-height: 1.7;
    }

    .note-card {
      background: #f8fafc;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-md);
      padding: 10px 12px;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      flex-direction: column;
      gap: 6px;
      position: relative;
    }
    .note-card:hover {
      border-color: #93c5fd;
      background: #eff6ff;
      transform: translateY(-1px);
    }
    .note-card.active {
      border-color: var(--primary);
      background: #e0f2fe;
      box-shadow: 0 2px 8px rgba(14,165,233,0.15);
    }
    .note-card.pinned {
      border-right: 4px solid #f59e0b;
      background: #fffbeb;
    }
    .note-card.pinned:hover {
      background: #fef3c7;
    }

    .note-card-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .note-title {
      font-size: 13px;
      font-weight: 700;
      color: var(--text-main);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      flex: 1;
    }
    .note-card-actions {
      display: flex;
      align-items: center;
      gap: 4px;
      opacity: 0.6;
      transition: opacity 0.2s;
    }
    .note-card:hover .note-card-actions { opacity: 1; }
    
    .note-action-btn {
      background: none;
      border: none;
      color: #64748b;
      cursor: pointer;
      font-size: 12px;
      padding: 3px 5px;
      border-radius: 4px;
      transition: all 0.15s;
    }
    .note-action-btn:hover {
      background: #e2e8f0;
      color: #0f172a;
    }
    .note-action-btn.delete-btn:hover {
      background: #fee2e2;
      color: #dc2626;
    }
    .note-action-btn.pin-btn.is-pinned {
      color: #d97706;
    }

    .note-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
      color: var(--text-muted);
    }

    .sidebar-footer {
      padding: 12px 14px;
      background: #f8fafc;
      border-top: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11.5px;
      color: var(--text-muted);
    }
    .clear-all-notes-btn {
      background: none;
      border: none;
      color: #dc2626;
      font-size: 11.5px;
      cursor: pointer;
      font-weight: 600;
      padding: 2px 6px;
      border-radius: 4px;
    }
    .clear-all-notes-btn:hover { background: #fee2e2; }

    /* ============================================================
       منطقة العمل الرئيسية (Main Chat View)
    ============================================================ */
    .main-wrapper {
      flex: 1;
      display: flex;
      flex-direction: column;
      height: 100vh;
      height: 100dvh;
      min-width: 0;
      position: relative;
    }

    header {
      background: #ffffff;
      border-bottom: 1px solid var(--border-color);
      padding: 10px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      box-shadow: var(--shadow-sm);
      z-index: 10;
    }

    .header-left, .header-right {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .menu-toggle-btn {
      background: #f1f5f9;
      color: #1e293b;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-sm);
      padding: 7px 12px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s;
    }
    .menu-toggle-btn:hover {
      background: #e2e8f0;
      border-color: #cbd5e1;
    }

    .brand-title {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .brand-title h1 {
      font-size: 18px;
      font-weight: 800;
      color: #1e3a8a;
      letter-spacing: -0.3px;
    }
    .brand-title .model-pill {
      background: #e0f2fe;
      color: #0369a1;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 20px;
      border: 1px solid #bae6fd;
    }

    .dev-profile-btn {
      background: linear-gradient(135deg, #eff6ff, #dbeafe);
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
      border-radius: 20px;
      padding: 5px 12px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 5px;
      transition: all 0.2s;
    }
    .dev-profile-btn:hover {
      background: #bfdbfe;
      transform: translateY(-1px);
    }

    .status-badge {
      font-size: 12px;
      font-weight: 700;
      padding: 5px 10px;
      border-radius: 20px;
      background: #f1f5f9;
      color: #475569;
      border: 1px solid #e2e8f0;
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .status-badge.streaming {
      background: #fef3c7;
      color: #b45309;
      border-color: #fde68a;
    }
    .status-badge.ready {
      background: #ecfdf5;
      color: #047857;
      border-color: #a7f3d0;
    }

    /* منطقة الرسائل */
    .chat-container {
      flex: 1;
      max-width: 900px;
      width: 100%;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      padding: 16px;
      overflow: hidden;
    }

    .chat-messages {
      flex: 1;
      overflow-y: auto;
      padding: 10px 6px 20px 6px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      scroll-behavior: smooth;
    }

    /* مظهر الرسائل */
    .msg-wrapper {
      display: flex;
      flex-direction: column;
      max-width: 86%;
      animation: fadeIn 0.25s ease-out;
    }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .msg-wrapper.user {
      align-self: flex-start;
      margin-right: 0;
    }
    .msg-wrapper.bot {
      align-self: flex-end;
      margin-left: 0;
      width: 100%;
      max-width: 92%;
    }

    .msg-bubble {
      padding: 14px 18px;
      border-radius: 16px;
      font-size: 14.5px;
      line-height: 1.8;
      position: relative;
      word-break: break-word;
      overflow-wrap: break-word;
    }

    .msg-wrapper.user .msg-bubble {
      background: linear-gradient(135deg, #1d4ed8, #2563eb);
      color: #ffffff;
      border-bottom-left-radius: 4px;
      box-shadow: 0 3px 10px rgba(37,99,235,0.2);
      font-weight: 600;
    }

    .msg-wrapper.bot .msg-bubble {
      background: #ffffff;
      color: #1e293b;
      border: 1px solid #e2e8f0;
      border-bottom-right-radius: 4px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.03);
    }

    /* تنسيقات Markdown الاحترافية للإجابات */
    .msg-content {
      line-height: 1.85;
    }
    .msg-content p {
      margin-bottom: 10px;
    }
    .msg-content p:last-child {
      margin-bottom: 0;
    }
    .msg-content h1, .msg-content h2, .msg-content h3, .msg-content h4 {
      color: #0f172a;
      margin: 16px 0 8px 0;
      font-weight: 800;
    }
    .msg-content h1 { font-size: 18px; }
    .msg-content h2 { font-size: 16.5px; }
    .msg-content h3 { font-size: 15px; }
    .msg-content ul, .msg-content ol {
      margin: 8px 20px 12px;
    }
    .msg-content li {
      margin-bottom: 4px;
    }
    .msg-content blockquote {
      border-right: 4px solid var(--primary);
      padding: 6px 14px;
      background: #f8fafc;
      color: #475569;
      margin: 10px 0;
      border-radius: 0 8px 8px 0;
      font-style: italic;
    }
    .msg-content table {
      border-collapse: collapse;
      width: 100%;
      margin: 12px 0;
      font-size: 13.5px;
      background: #fff;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
    }
    .msg-content th, .msg-content td {
      border: 1px solid #e2e8f0;
      padding: 8px 12px;
      text-align: right;
    }
    .msg-content th {
      background: #f1f5f9;
      font-weight: 700;
      color: #1e293b;
    }
    .msg-content a {
      color: var(--primary);
      text-decoration: underline;
    }

    /* تصميم كتل الأكواد البرمجية الفاخر */
    .code-container {
      position: relative;
      margin: 12px 0;
      border-radius: 10px;
      overflow: hidden;
      background: #282c34;
      border: 1px solid #3e4451;
      direction: ltr;
      text-align: left;
    }
    .code-header {
      background: #21252b;
      padding: 6px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      font-family: monospace;
      font-size: 12px;
      color: #abb2bf;
    }
    .code-copy-btn {
      background: rgba(255,255,255,0.1);
      border: 1px solid rgba(255,255,255,0.18);
      color: #e5e7eb;
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 5px;
      cursor: pointer;
      transition: all 0.2s;
      font-family: 'Cairo', sans-serif;
    }
    .code-copy-btn:hover { background: rgba(255,255,255,0.22); color: #fff; }
    .code-copy-btn.copied { background: #166534; color: #bbf7d0; border-color: #22c55e; }

    pre code.hljs {
      padding: 12px 14px;
      font-family: 'Consolas', 'Courier New', monospace;
      font-size: 13.5px;
      line-height: 1.6;
      border-radius: 0 0 10px 10px;
      overflow-x: auto;
    }

    /* شريط أدوات الرسالة السفلية */
    .msg-toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 6px;
      padding: 0 4px;
      opacity: 0.85;
    }
    .msg-btn {
      background: #f1f5f9;
      border: 1px solid #e2e8f0;
      color: #475569;
      font-size: 11.5px;
      padding: 3px 8px;
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 4px;
      font-weight: 600;
      transition: all 0.15s;
    }
    .msg-btn:hover {
      background: #e2e8f0;
      color: #0f172a;
    }
    .msg-btn.copied { background: #dcfce7; color: #15803d; border-color: #86efac; }

    /* تأثير الكتابة المباشرة البصري (Streaming Cursor) */
    .streaming-cursor {
      display: inline-block;
      width: 8px;
      height: 16px;
      background: var(--primary);
      margin-right: 3px;
      animation: blink 0.8s infinite;
      vertical-align: middle;
      border-radius: 2px;
    }
    @keyframes blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0; }
    }

    /* زر التمرير للأسفل السريع */
    .scroll-bottom-btn {
      position: absolute;
      bottom: 80px;
      left: 50%;
      transform: translateX(-50%);
      background: #ffffff;
      border: 1px solid #cbd5e1;
      box-shadow: var(--shadow-md);
      padding: 6px 14px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 700;
      color: #334155;
      cursor: pointer;
      display: none;
      align-items: center;
      gap: 6px;
      z-index: 15;
      transition: all 0.2s;
    }
    .scroll-bottom-btn:hover { background: #f8fafc; color: var(--primary); }

    /* منطقة الإدخال الفاخرة */
    .input-section {
      background: #ffffff;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      padding: 8px 12px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.05);
      display: flex;
      flex-direction: column;
      gap: 8px;
      position: relative;
    }

    .input-row {
      display: flex;
      align-items: flex-end;
      gap: 8px;
    }

    textarea#questionInput {
      flex: 1;
      padding: 8px 10px;
      border: none;
      outline: none;
      resize: none;
      font-size: 14.5px;
      max-height: 140px;
      min-height: 44px;
      line-height: 1.6;
      color: var(--text-main);
      background: transparent;
    }

    .action-controls {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .btn-send {
      background: linear-gradient(135deg, #1d4ed8, #2563eb);
      color: white;
      border: none;
      border-radius: var(--radius-md);
      padding: 10px 18px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s;
      box-shadow: 0 2px 6px rgba(37,99,235,0.25);
    }
    .btn-send:hover {
      background: linear-gradient(135deg, #1e40af, #1d4ed8);
      transform: translateY(-1px);
    }

    .btn-stop {
      background: #dc2626;
      color: white;
      border: none;
      border-radius: var(--radius-md);
      padding: 10px 18px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      display: none;
      align-items: center;
      gap: 6px;
      transition: background 0.2s;
    }
    .btn-stop:hover { background: #b91c1c; }

    /* شريط تذييل المطور السفلي */
    .app-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 6px 0 6px;
      font-size: 12px;
      color: var(--text-muted);
    }
    .app-footer a {
      color: var(--primary);
      text-decoration: none;
      font-weight: 700;
    }
    .app-footer a:hover { text-decoration: underline; }

    /* طبقة التعتيم للشاشات الصغيرة */
    .backdrop-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.45);
      backdrop-filter: blur(2px);
      z-index: 95;
    }

    /* ============================================================
       نافذة بروفايل المطور المنبثقة (Developer Profile Modal)
    ============================================================ */
    .modal-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(4px);
      z-index: 200;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    .modal-overlay.active {
      display: flex;
    }

    .dev-modal {
      background: #ffffff;
      width: 100%;
      max-width: 480px;
      border-radius: var(--radius-lg);
      box-shadow: 0 10px 30px rgba(0,0,0,0.2);
      overflow: hidden;
      animation: modalPop 0.25s ease-out;
      border: 1px solid var(--border-color);
    }
    @keyframes modalPop {
      from { transform: scale(0.92); opacity: 0; }
      to { transform: scale(1); opacity: 1; }
    }

    .dev-modal-header {
      background: linear-gradient(135deg, #1e3a8a, #2563eb);
      color: white;
      padding: 24px 20px;
      text-align: center;
      position: relative;
    }
    .dev-modal-header .avatar-box {
      width: 72px;
      height: 72px;
      background: #ffffff;
      color: #1d4ed8;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 32px;
      font-weight: 800;
      margin: 0 auto 10px;
      box-shadow: 0 4px 10px rgba(0,0,0,0.15);
      border: 3px solid #bfdbfe;
    }
    .dev-modal-header h3 { font-size: 20px; font-weight: 800; }
    .dev-modal-header p { font-size: 13px; opacity: 0.9; margin-top: 4px; }
    .modal-close-btn {
      position: absolute;
      top: 14px;
      left: 14px;
      background: rgba(255,255,255,0.2);
      border: none;
      color: white;
      width: 30px;
      height: 30px;
      border-radius: 50%;
      cursor: pointer;
      font-size: 16px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .modal-close-btn:hover { background: rgba(255,255,255,0.35); }

    .dev-modal-body {
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      font-size: 13.5px;
      line-height: 1.7;
      color: #334155;
    }
    .skill-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
    }
    .skill-tag {
      background: #eff6ff;
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
      padding: 3px 10px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 700;
    }
    .dev-modal-footer {
      padding: 14px 20px;
      background: #f8fafc;
      border-top: 1px solid var(--border-color);
      display: flex;
      justify-content: center;
    }
    .cv-btn-large {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: #1d4ed8;
      color: white;
      text-decoration: none;
      padding: 10px 22px;
      border-radius: var(--radius-md);
      font-size: 14px;
      font-weight: 700;
      transition: all 0.2s;
      box-shadow: 0 3px 10px rgba(29,78,216,0.25);
    }
    .cv-btn-large:hover { background: #1e40af; transform: translateY(-1px); }

    /* ============================================================
       تجاوب الهواتف والشاشات الصغيرة (Mobile Responsive)
    ============================================================ */
    @media (max-width: 768px) {
      .sidebar {
        position: fixed;
        top: 0;
        bottom: 0;
        right: 0;
        width: 84%;
        max-width: 320px;
        transform: translateX(100%);
        box-shadow: -4px 0 20px rgba(0,0,0,0.15);
      }
      .sidebar.open {
        transform: translateX(0);
      }
      .close-sidebar-btn { display: inline-block; }
      .backdrop-overlay.show { display: block; }

      header {
        padding: 8px 12px;
      }
      .brand-title h1 { font-size: 16px; }
      .model-pill { display: none; }
      .dev-profile-btn { padding: 4px 10px; font-size: 11.5px; }

      .chat-container { padding: 8px; }
      .msg-wrapper { max-width: 95%; }
      .msg-wrapper.bot { max-width: 98%; }
      .msg-bubble { padding: 10px 14px; font-size: 14px; }

      .input-section { padding: 6px 10px; }
      .btn-send, .btn-stop { padding: 8px 14px; font-size: 13px; }
    }
  </style>
</head>
<body>

  <!-- طبقة التعتيم للموبايل -->
  <div class="backdrop-overlay" id="backdropOverlay" onclick="toggleSidebar(false)"></div>

  <!-- الشريط الجانبي: دفتر الملاحظات المطور -->
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <h2>
        <span>📒 دفتر الملاحظات</span>
        <span class="badge" id="notesCountBadge">0</span>
      </h2>
      <button class="close-sidebar-btn" onclick="toggleSidebar(false)">✕</button>
    </div>

    <div class="sidebar-actions">
      <button class="new-chat-btn" onclick="createNewChat()">
        <span>➕</span>
        <span>محادثة جديدة (حفظ الحالية)</span>
      </button>

      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input type="text" id="notesSearchInput" placeholder="بحث في الملاحظات..." oninput="handleNotesSearch()">
      </div>
    </div>

    <div class="notes-list" id="notesListContainer">
      <!-- يتم تعبئة بطاقات الملاحظات بواسطة JavaScript -->
    </div>

    <div class="sidebar-footer">
      <span id="savedStatsText">لا توجد محادثات محفوظة</span>
      <button class="clear-all-notes-btn" onclick="confirmClearAllNotes()">مسح الكل</button>
    </div>
  </aside>

  <!-- الواجهة الرئيسية للمحادثة -->
  <div class="main-wrapper">
    <header>
      <div class="header-right">
        <button class="menu-toggle-btn" onclick="toggleSidebar(true)">
          <span>📒</span>
          <span>الملاحظات</span>
        </button>
        <div class="brand-title">
          <h1>Amro AI CHAT ✨</h1>
          <span class="model-pill">Gemini 3 Turbo</span>
        </div>
      </div>

      <div class="header-left">
        <button class="dev-profile-btn" onclick="openDevModal()">
          <span>👨‍💻</span>
          <span>المطور</span>
        </button>
        <div class="status-badge ready" id="statusBadge">جاهز ✨</div>
      </div>
    </header>

    <div class="chat-container">
      <div class="chat-messages" id="chatMessagesArea">
        <!-- الرسائل هنا -->
      </div>

      <button class="scroll-bottom-btn" id="scrollBottomBtn" onclick="scrollToBottom(true)">
        <span>⬇️ التمرير لأسفل</span>
      </button>

      <div class="input-section">
        <div class="input-row">
          <textarea 
            id="questionInput" 
            placeholder="اكتب رسالتك لـ Amro AI... (Enter للإرسال، Shift+Enter لسطر جديد)"
            rows="1"></textarea>
          
          <div class="action-controls">
            <button id="sendBtn" class="btn-send" onclick="handleSendMessage()">
              <span>إرسال</span>
              <span>🚀</span>
            </button>
            <button id="stopBtn" class="btn-stop" onclick="handleStopGeneration()">
              <span>إيقاف</span>
              <span>⏹️</span>
            </button>
          </div>
        </div>

        <div class="app-footer">
          <span>👨‍💻 تطوير: <strong>عمرو الشرافي</strong> — مهندس برمجيات وذكاء اصطناعي</span>
          <a href="https://amrocv.vercel.app" target="_blank" rel="noopener noreferrer">السيرة الذاتية ومعرض الأعمال ↗</a>
        </div>
      </div>
    </div>
  </div>

  <!-- نافذة بروفايل المطور (Modal) -->
  <div class="modal-overlay" id="devModalOverlay" onclick="closeDevModalOnBg(event)">
    <div class="dev-modal">
      <div class="dev-modal-header">
        <button class="modal-close-btn" onclick="closeDevModal()">✕</button>
        <div class="avatar-box">AS</div>
        <h3>عمرو الشرافي | Amro Alshurafy</h3>
        <p>Software Engineer & AI Solutions Developer</p>
      </div>
      <div class="dev-modal-body">
        <p>
          مرحباً بك! أنا <strong>عمرو الشرافي</strong>، مهندس برمجيات متخصص في بناء تطبيقات الهاتف والويب وحلول الذكاء الاصطناعي التوليدي.
        </p>
        <p>
          🎓 <strong>التعليم:</strong> طالب تقنية معلومات (IT) — جامعة شبوة.
        </p>
        <div>
          <strong>💼 المهارات والتقنيات:</strong>
          <div class="skill-tags">
            <span class="skill-tag">Flutter & Dart</span>
            <span class="skill-tag">React.js</span>
            <span class="skill-tag">Python & Flask</span>
            <span class="skill-tag">Google Gemini AI</span>
            <span class="skill-tag">RESTful APIs</span>
            <span class="skill-tag">Clean Architecture</span>
          </div>
        </div>
        <p style="font-size: 12.5px; color: #64748b; background: #f8fafc; padding: 10px; border-radius: 8px; border: 1px dashed #cbd5e1;">
          💡 تم تصميم <strong>Amro AI CHAT</strong> ليوفر تجربة محادثة فائقة السرعة مع بث مباشر للردود، وحماية أمنية للبيانات، ودفتر ملاحظات متطور لتنظيم أفكارك.
        </p>
      </div>
      <div class="dev-modal-footer">
        <a class="cv-btn-large" href="https://amrocv.vercel.app" target="_blank" rel="noopener noreferrer">
          <span>🌐 زيارة معرض الأعمال والسيرة الذاتية (CV)</span>
          <span>↗</span>
        </a>
      </div>
    </div>
  </div>

  <!-- منطق التطبيق التفاعلي (JavaScript) -->
  <script>
    // تهيئة مكتبة Marked لتنسيق Markdown الآمن
    marked.setOptions({
      gfm: true,
      breaks: true
    });

    const STORAGE_KEYS = {
      NOTES: "amro_ai_notes_v2",
      ACTIVE_CHAT: "amro_ai_active_chat_v2"
    };

    // حالة التطبيق العامة
    let currentHistory = [];
    let currentNoteId = null;
    let isGenerating = false;
    let abortController = null;
    let notes = loadSavedNotes();
    let searchTerm = "";

    // التعامل الآمن مع localStorage
    function loadSavedNotes() {
      try {
        const raw = localStorage.getItem(STORAGE_KEYS.NOTES);
        return raw ? JSON.parse(raw) : [];
      } catch (e) {
        console.error("خطأ في قراءة الملاحظات:", e);
        return [];
      }
    }

    function saveNotesToStorage() {
      try {
        localStorage.setItem(STORAGE_KEYS.NOTES, JSON.stringify(notes));
      } catch (e) {
        console.error("خطأ في تخزين الملاحظات:", e);
      }
    }

    function saveActiveChatToStorage() {
      try {
        if (currentHistory && currentHistory.length > 0) {
          localStorage.setItem(STORAGE_KEYS.ACTIVE_CHAT, JSON.stringify({
            history: currentHistory,
            currentNoteId: currentNoteId,
            timestamp: Date.now()
          }));
        } else {
          localStorage.removeItem(STORAGE_KEYS.ACTIVE_CHAT);
        }
      } catch (e) {
        console.error("خطأ في حفظ المحادثة النشطة:", e);
      }
    }

    // فتح وإغلاق الشريط الجانبي
    function toggleSidebar(show) {
      const sidebar = document.getElementById("sidebar");
      const overlay = document.getElementById("backdropOverlay");
      if (show) {
        sidebar.classList.add("open");
        overlay.classList.add("show");
      } else {
        sidebar.classList.remove("open");
        overlay.classList.remove("show");
      }
    }

    // فتح وإغلاق نافذة المطور
    function openDevModal() {
      document.getElementById("devModalOverlay").classList.add("active");
    }
    function closeDevModal() {
      document.getElementById("devModalOverlay").classList.remove("active");
    }
    function closeDevModalOnBg(e) {
      if (e.target.id === "devModalOverlay") closeDevModal();
    }

    // تحديث شارة الحالة
    function setStatus(text, type = "ready") {
      const badge = document.getElementById("statusBadge");
      badge.textContent = text;
      badge.className = "status-badge " + type;
    }

    // نسخ النصوص للحافظة
    function copyText(text, btnElement, successMsg = "✅ تم النسخ!") {
      const origText = btnElement.innerHTML;
      navigator.clipboard.writeText(text).then(() => {
        btnElement.innerHTML = successMsg;
        btnElement.classList.add("copied");
        setTimeout(() => {
          btnElement.innerHTML = origText;
          btnElement.classList.remove("copied");
        }, 1800);
      }).catch(() => {
        // Fallback
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.top = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        btnElement.innerHTML = successMsg;
        btnElement.classList.add("copied");
        setTimeout(() => {
          btnElement.innerHTML = origText;
          btnElement.classList.remove("copied");
        }, 1800);
      });
    }

    // تنسيق وتطهير Markdown ضد ثغرات XSS
    function renderMarkdownToHtml(markdownText) {
      const rawHtml = marked.parse(markdownText || "");
      return DOMPurify.sanitize(rawHtml, {
        ADD_ATTR: ['target', 'rel']
      });
    }

    // إنشاء كتلة رسالة
    function createMessageElement(role, text = "") {
      const area = document.getElementById("chatMessagesArea");
      const wrapper = document.createElement("div");
      wrapper.className = `msg-wrapper ${role}`;

      const bubble = document.createElement("div");
      bubble.className = "msg-bubble";

      const content = document.createElement("div");
      content.className = "msg-content";

      if (role === "user") {
        content.textContent = text;
      } else {
        content.innerHTML = renderMarkdownToHtml(text);
      }
      bubble.appendChild(content);

      // شريط الأدوات لرسائل الذكاء الاصطناعي
      if (role === "bot") {
        const toolbar = document.createElement("div");
        toolbar.className = "msg-toolbar";

        const copyBtn = document.createElement("button");
        copyBtn.className = "msg-btn";
        copyBtn.innerHTML = "<span>📋</span><span>نسخ الرد</span>";
        copyBtn.onclick = () => copyText(wrapper.dataset.fullText || "", copyBtn);

        const speakBtn = document.createElement("button");
        speakBtn.className = "msg-btn";
        speakBtn.innerHTML = "<span>🔊</span><span>استماع</span>";
        speakBtn.onclick = () => speakMessage(wrapper.dataset.fullText || "");

        toolbar.appendChild(copyBtn);
        if ('speechSynthesis' in window) {
          toolbar.appendChild(speakBtn);
        }
        bubble.appendChild(toolbar);
      }

      wrapper.appendChild(bubble);
      wrapper.dataset.fullText = text;
      area.appendChild(wrapper);

      if (role === "bot") {
        enhanceCodeBlocks(wrapper);
      }

      scrollToBottom();
      return { wrapper, bubble, content };
    }

    // تلوين وإضافة أزرار النسخ لكتل الأكواد البرمجية
    function enhanceCodeBlocks(container) {
      container.querySelectorAll("pre code").forEach((codeEl) => {
        // تلوين الكود عبر Highlight.js
        hljs.highlightElement(codeEl);

        const pre = codeEl.parentElement;
        if (pre.parentElement.classList.contains("code-container")) return;

        // استخراج اسم لغة البرمجة
        let langName = "code";
        codeEl.classList.forEach(cls => {
          if (cls.startsWith("language-")) {
            langName = cls.replace("language-", "");
          }
        });

        const codeContainer = document.createElement("div");
        codeContainer.className = "code-container";

        const header = document.createElement("div");
        header.className = "code-header";
        header.innerHTML = `<span>${langName.toUpperCase()}</span>`;

        const copyBtn = document.createElement("button");
        copyBtn.className = "code-copy-btn";
        copyBtn.textContent = "📋 نسخ الكود";
        copyBtn.onclick = () => copyText(codeEl.innerText, copyBtn, "✅ نُسخ!");

        header.appendChild(copyBtn);
        pre.parentNode.insertBefore(codeContainer, pre);
        codeContainer.appendChild(header);
        codeContainer.appendChild(pre);
      });
    }

    // التمرير التلقائي لأسفل
    function scrollToBottom(force = false) {
      const area = document.getElementById("chatMessagesArea");
      const isNearBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 120;
      if (force || isNearBottom) {
        area.scrollTop = area.scrollHeight;
      }
    }

    // مراقبة التمرير لإظهار زر التمرير السريع
    document.getElementById("chatMessagesArea").addEventListener("scroll", () => {
      const area = document.getElementById("chatMessagesArea");
      const btn = document.getElementById("scrollBottomBtn");
      if (area.scrollHeight - area.scrollTop - area.clientHeight > 200) {
        btn.style.display = "flex";
      } else {
        btn.style.display = "none";
      }
    });

    // النطق الصوتي للرسالة (Speech Synthesis)
    function speakMessage(text) {
      if (!('speechSynthesis' in window)) return;
      window.speechSynthesis.cancel();
      // تنظيف الرموز الخاصة بالماركداون قبل القراءة
      const clean = text.replace(/[*#`_~]/g, "").slice(0, 500);
      const utter = new SpeechSynthesisUtterance(clean);
      utter.lang = "ar-SA";
      window.speechSynthesis.speak(utter);
    }

    // ============================================================
    // إدارة دفتر الملاحظات (Notebook Management)
    // ============================================================
    function renderNotesList() {
      const container = document.getElementById("notesListContainer");
      container.innerHTML = "";

      let filtered = notes;
      if (searchTerm.trim()) {
        const q = searchTerm.trim().toLowerCase();
        filtered = notes.filter(n => {
          const inTitle = n.title && n.title.toLowerCase().includes(q);
          const inChat = n.history && n.history.some(m => 
            m.parts && m.parts[0] && m.parts[0].text && m.parts[0].text.toLowerCase().includes(q)
          );
          return inTitle || inChat;
        });
      }

      // ترتيب الملاحظات: المثبتة أولاً (Pinned)، ثم الأحدث
      filtered.sort((a, b) => {
        if (a.pinned && !b.pinned) return -1;
        if (!a.pinned && b.pinned) return 1;
        return (b.updatedAt || b.id) - (a.updatedAt || a.id);
      });

      document.getElementById("notesCountBadge").textContent = notes.length;
      document.getElementById("savedStatsText").textContent = 
        notes.length ? `${notes.length} محادثة محفوظة` : "لا توجد محادثات محفوظة";

      if (filtered.length === 0) {
        container.innerHTML = `
          <div class="empty-notes">
            ${searchTerm.trim() ? "لا توجد نتائج مطابقة لبحثك 🔍" : "دفتر الملاحظات فارغ حالياً 📭<br>ابدأ بمحادثة وسيتم حفظها هنا تلقائياً عند الضغط على 'محادثة جديدة'."}
          </div>
        `;
        return;
      }

      filtered.forEach(note => {
        const card = document.createElement("div");
        card.className = `note-card ${note.id === currentNoteId ? 'active' : ''} ${note.pinned ? 'pinned' : ''}`;
        
        const count = Array.isArray(note.history) ? Math.floor(note.history.length / 2) : 0;
        
        card.innerHTML = `
          <div class="note-card-top">
            <div class="note-title" title="${escapeHtml(note.title)}">${note.pinned ? '📌 ' : '💬 '}${escapeHtml(note.title)}</div>
            <div class="note-card-actions">
              <button class="note-action-btn pin-btn ${note.pinned ? 'is-pinned' : ''}" title="${note.pinned ? 'إلغاء التثبيت' : 'تثبيت في الأعلى'}" onclick="togglePinNote(event, ${note.id})">📌</button>
              <button class="note-action-btn" title="إعادة تسمية" onclick="renameNotePrompt(event, ${note.id})">✏️</button>
              <button class="note-action-btn" title="تصدير المحادثة" onclick="exportNoteMenu(event, ${note.id})">📥</button>
              <button class="note-action-btn delete-btn" title="حذف" onclick="deleteNoteItem(event, ${note.id})">🗑️</button>
            </div>
          </div>
          <div class="note-meta">
            <span>🕒 ${note.date || 'اليوم'}</span>
            <span>${count} أسئلة وأجوبة</span>
          </div>
        `;

        card.onclick = (e) => {
          if (e.target.closest(".note-card-actions")) return;
          loadNoteById(note.id);
        };

        container.appendChild(card);
      });
    }

    function escapeHtml(str) {
      if (!str) return "";
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function handleNotesSearch() {
      searchTerm = document.getElementById("notesSearchInput").value;
      renderNotesList();
    }

    // تصدير المحادثة الحالية للدفتر
    function autoSaveCurrentChatToNotes() {
      if (!currentHistory || currentHistory.length === 0) return;

      const firstUserMsg = currentHistory.find(m => m.role === "user")?.parts?.[0]?.text || "محادثة جديدة";
      const cleanTitle = firstUserMsg.length > 36 ? firstUserMsg.substring(0, 36) + "..." : firstUserMsg;
      const nowStr = new Date().toLocaleTimeString("ar-EG", { hour: "2-digit", minute: "2-digit" });

      if (currentNoteId) {
        const idx = notes.findIndex(n => n.id === currentNoteId);
        if (idx !== -1) {
          notes[idx].history = JSON.parse(JSON.stringify(currentHistory));
          notes[idx].updatedAt = Date.now();
          notes[idx].date = nowStr;
        } else {
          currentNoteId = null;
        }
      }

      if (!currentNoteId) {
        const newId = Date.now();
        notes.unshift({
          id: newId,
          title: cleanTitle,
          date: nowStr,
          pinned: false,
          updatedAt: Date.now(),
          history: JSON.parse(JSON.stringify(currentHistory))
        });
        currentNoteId = newId;
      }

      saveNotesToStorage();
      saveActiveChatToStorage();
      renderNotesList();
    }

    function loadNoteById(id) {
      if (isGenerating) handleStopGeneration();
      autoSaveCurrentChatToNotes();

      const target = notes.find(n => n.id === id);
      if (!target) return;

      currentNoteId = target.id;
      currentHistory = JSON.parse(JSON.stringify(target.history || []));
      renderCurrentHistory();
      saveActiveChatToStorage();
      renderNotesList();

      setStatus("📒 ملاحظة معروضة", "ready");
      if (window.innerWidth <= 768) toggleSidebar(false);
    }

    function togglePinNote(event, id) {
      event.stopPropagation();
      const n = notes.find(item => item.id === id);
      if (n) {
        n.pinned = !n.pinned;
        saveNotesToStorage();
        renderNotesList();
      }
    }

    function renameNotePrompt(event, id) {
      event.stopPropagation();
      const n = notes.find(item => item.id === id);
      if (!n) return;
      const newTitle = prompt("أدخل العنوان الجديد للمحادثة:", n.title);
      if (newTitle && newTitle.trim()) {
        n.title = newTitle.trim();
        saveNotesToStorage();
        renderNotesList();
      }
    }

    function exportNoteMenu(event, id) {
      event.stopPropagation();
      const n = notes.find(item => item.id === id);
      if (!n || !n.history) return;

      const format = prompt("اختر صيغة التصدير:\n1 = ملف Markdown (.md)\n2 = ملف نصي عادي (.txt)\n3 = طباعة المحادثة", "1");
      if (!format) return;

      let content = `# ${n.title}\nتاريخ المحادثة: ${n.date}\nمطور النظام: عمرو الشرافي (Amro Alshurafy)\n\n---\n\n`;
      n.history.forEach(m => {
        const sender = m.role === "user" ? "👤 أنت" : "✨ Amro AI";
        const text = m.parts?.[0]?.text || "";
        content += `### ${sender}:\n${text}\n\n`;
      });

      if (format === "1") {
        downloadFile(`${n.title}.md`, content, "text/markdown");
      } else if (format === "2") {
        downloadFile(`${n.title}.txt`, content, "text/plain");
      } else if (format === "3") {
        window.print();
      }
    }

    function downloadFile(filename, text, type) {
      const blob = new Blob([text], { type: `${type};charset=utf-8` });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }

    function deleteNoteItem(event, id) {
      event.stopPropagation();
      if (!confirm("هل أنت متأكد من حذف هذه المحادثة من دفتر الملاحظات؟")) return;

      notes = notes.filter(n => n.id !== id);
      saveNotesToStorage();

      if (currentNoteId === id) {
        currentNoteId = null;
        currentHistory = [];
        saveActiveChatToStorage();
        showWelcomeMessage();
      }
      renderNotesList();
    }

    function confirmClearAllNotes() {
      if (notes.length === 0) return;
      if (confirm("⚠️ تحذير: هل تريد حقاً مسح كافة المحادثات المحفوظة في دفتر الملاحظات؟")) {
        notes = [];
        currentNoteId = null;
        currentHistory = [];
        saveNotesToStorage();
        saveActiveChatToStorage();
        showWelcomeMessage();
        renderNotesList();
      }
    }

    function createNewChat() {
      if (isGenerating) handleStopGeneration();
      autoSaveCurrentChatToNotes();

      currentHistory = [];
      currentNoteId = null;
      saveActiveChatToStorage();
      showWelcomeMessage();
      renderNotesList();
      setStatus("جاهز ✨", "ready");

      if (window.innerWidth <= 768) toggleSidebar(false);
      document.getElementById("questionInput").focus();
    }

    function renderCurrentHistory() {
      const area = document.getElementById("chatMessagesArea");
      area.innerHTML = "";

      if (!currentHistory || currentHistory.length === 0) {
        showWelcomeMessage();
        return;
      }

      currentHistory.forEach(msg => {
        const text = msg.parts?.[0]?.text || "";
        if (text) {
          createMessageElement(msg.role === "user" ? "user" : "bot", text);
        }
      });
      scrollToBottom(true);
    }

    function showWelcomeMessage() {
      const area = document.getElementById("chatMessagesArea");
      area.innerHTML = "";
      const welcomeText = 
        `أهلاً بك في **Amro AI CHAT** 👋✨\n\n` +
        `أنا مساعدك الذكي فائق التطور، طُوّرت بفخر على يد المطور **عمرو الشرافي (Amro Alshurafy)** 🚀\n\n` +
        `💡 **مميزات جديدة في هذا الإصدار:**\n` +
        `* ⚡ **بث مباشر للإجابات (Streaming):** طباعة ذكية وسريعة وفورية.\n` +
        `* 🛡️ **حماية أمنية مشددة:** تطهير المدخلات وتحديد معدل الطلبات.\n` +
        `* 📒 **دفتر ملاحظات متطور:** بحث فوري، تثبيت المحادثات 📌، إعادة تسمية ✏️، وتصدير كامل 📥.\n` +
        `* 📋 **نسخ تلقائي للأكواد البرمجية** مع تلوين دقيق لكافة لغات البرمجة.\n\n` +
        `تفضل بطرح أي سؤال أو موضوع ترغب في مناقشته! ✨`;
      createMessageElement("bot", welcomeText);
    }

    // ============================================================
    // إرسال الرسالة ودعم البث الحي (Streaming - SSE)
    // ============================================================
    async function handleSendMessage() {
      if (isGenerating) return;

      const input = document.getElementById("questionInput");
      const question = input.value.trim();
      if (!question) return;

      // إضافة رسالة المستخدم للواجهة والسياق
      createMessageElement("user", question);
      input.value = "";
      adjustTextareaHeight(input);

      // تحديث حالة الواجهة
      setGeneratingUI(true);
      setStatus("يكتب الآن ✍️", "streaming");

      // تجهيز رسالة الذكاء الاصطناعي للبث
      const botElements = createMessageElement("bot", "");
      const cursorSpan = document.createElement("span");
      cursorSpan.className = "streaming-cursor";
      botElements.content.appendChild(cursorSpan);

      abortController = new AbortController();
      let fullAnswer = "";

      try {
        const response = await fetch("/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question: question,
            history: currentHistory
          }),
          signal: abortController.signal
        });

        // التعامل مع تجاوز حد الطلبات (Rate Limit) أو أخطاء الخادم
        if (response.status === 429) {
          const errData = await response.json();
          throw new Error(errData.error || "تم تجاوز الحد المسموح للطلبات المؤقتة (Rate Limit)، يرجى الانتظار قليلاً.");
        }

        if (!response.ok) {
          const errData = await response.json().catch(() => ({}));
          throw new Error(errData.error || `خطأ في الخادم (${response.status})`);
        }

        // قراءة دفق الأحداث المرسلة من الخادم (Server-Sent Events)
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop(); // الاحتفاظ بالسطر غير المكتمل في المخزن

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data: ")) continue;
            const dataStr = trimmed.slice(6).trim();

            if (dataStr === "[DONE]") {
              break;
            }

            try {
              const parsed = JSON.parse(dataStr);
              if (parsed.error) {
                throw new Error(parsed.error);
              }
              if (parsed.chunk) {
                fullAnswer += parsed.chunk;
                botElements.content.innerHTML = renderMarkdownToHtml(fullAnswer);
                botElements.content.appendChild(cursorSpan);
                botElements.wrapper.dataset.fullText = fullAnswer;
                scrollToBottom();
              }
            } catch (jsonErr) {
              // تجاهل السطور غير المنسقة
            }
          }
        }

        // إزالة مؤشر الكتابة وإثراء كتل الأكواد بعد اكتمال الرد
        cursorSpan.remove();
        botElements.content.innerHTML = renderMarkdownToHtml(fullAnswer);
        botElements.wrapper.dataset.fullText = fullAnswer;
        enhanceCodeBlocks(botElements.wrapper);

        // حفظ في التاريخ والسياق
        currentHistory.push({ role: "user", parts: [{ text: question }] });
        currentHistory.push({ role: "model", parts: [{ text: fullAnswer }] });

        autoSaveCurrentChatToNotes();
        setStatus(currentNoteId ? "📒 محفوظة في الدفتر" : "متصل ✅", "ready");

      } catch (err) {
        cursorSpan.remove();
        if (err.name === "AbortError") {
          botElements.content.innerHTML = renderMarkdownToHtml(
            (fullAnswer ? fullAnswer + "\n\n" : "") + "*(تم إيقاف توليد الإجابة بناءً على طلبك ⏹️)*"
          );
          setStatus("تم الإيقاف ⏹️", "ready");
          if (fullAnswer) {
            currentHistory.push({ role: "user", parts: [{ text: question }] });
            currentHistory.push({ role: "model", parts: [{ text: fullAnswer }] });
            autoSaveCurrentChatToNotes();
          }
        } else {
          botElements.content.innerHTML = `<span style="color: #dc2626; font-weight: 700;">⚠️ عذراً: ${escapeHtml(err.message)}</span>`;
          setStatus("خطأ ❌", "ready");
        }
      } finally {
        abortController = null;
        setGeneratingUI(false);
        input.focus();
      }
    }

    function handleStopGeneration() {
      if (abortController) {
        abortController.abort();
        abortController = null;
      }
    }

    function setGeneratingUI(active) {
      isGenerating = active;
      document.getElementById("sendBtn").style.display = active ? "none" : "flex";
      document.getElementById("stopBtn").style.display = active ? "flex" : "none";
    }

    // التوسيع التلقائي لحقل الإدخال
    const textarea = document.getElementById("questionInput");
    function adjustTextareaHeight(el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 140) + "px";
    }
    textarea.addEventListener("input", function() {
      adjustTextareaHeight(this);
    });

    textarea.addEventListener("keydown", function(e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSendMessage();
      }
    });

    // تهيئة التطبيق عند الفتح
    function initApplication() {
      renderNotesList();

      try {
        const saved = localStorage.getItem(STORAGE_KEYS.ACTIVE_CHAT);
        if (saved) {
          const parsed = JSON.parse(saved);
          if (parsed && Array.isArray(parsed.history) && parsed.history.length > 0) {
            currentHistory = parsed.history;
            currentNoteId = parsed.currentNoteId || null;
            renderCurrentHistory();
            renderNotesList();
            setStatus(currentNoteId ? "📒 محفوظة في الدفتر" : "جاهز ✨", "ready");
            return;
          }
        }
      } catch (e) {
        console.warn("خطأ في قراءة المحادثة السابقة:", e);
      }

      showWelcomeMessage();
    }

    window.addEventListener("DOMContentLoaded", initApplication);
  </script>
</body>
</html>
"""

# ==========================================
# 5. مسارات الـ Backend وواجهات الـ API
# ==========================================

@app.after_request
def add_security_headers(response):
    """إضافة ترويسات أمنية لحماية التطبيق"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response

@app.route("/")
def home():
    """عرض الواجهة التفاعلية الرئيسية"""
    return render_template_string(HTML_TEMPLATE)

@app.route("/chat/stream", methods=["POST"])
def chat_stream():
    """
    مسار البث المباشر (SSE Streaming):
    - يطبق فحص الـ Rate Limiting بدقة.
    - يتحقق من سلامة المدخلات وتطهيرها.
    - يبث الإجابة كلمة بكلمة مع آلية Fallback ذكية للنماذج.
    """
    client_ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(client_ip)
    if not allowed:
        return jsonify({
            "error": f"تم تجاوز الحد المسموح للطلبات المؤقتة (Rate Limit). يرجى الانتظار {retry_after} ثانية."
        }), 429, {"Retry-After": str(retry_after)}

    api_key = get_api_key()
    if not api_key:
        return jsonify({
            "error": "مفتاح Gemini API غير مضبوط بشكل صحيح في ملف .env أو متغيرات البيئة."
        }), 500

    validated, err_msg = validate_and_sanitize_payload(request.json or {})
    if err_msg:
        return jsonify({"error": err_msg}), 400

    question, history = validated

    # بناء سياق الرسائل المتوافق مع Gemini API
    contents = history + [{"role": "user", "parts": [{"text": question}]}]

    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_INSTRUCTION_TEXT}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096
        }
    }

    def generate_events():
        # محاولة الاتصال بالنماذج حسب ترتيب الأولوية
        model_errors = []
        stream_success = False

        for model in PRIMARY_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
            headers = {
                "x-goog-api-key": api_key,
                "Content-Type": "application/json"
            }

            try:
                res = requests.post(url, headers=headers, json=payload, stream=True, timeout=18)
                if not res.ok:
                    try:
                        err_json = res.json()
                        err_text = err_json.get("error", {}).get("message", res.text[:200])
                    except Exception:
                        err_text = res.text[:200]
                    model_errors.append(f"{model}: {err_text}")
                    # في حال كان الخطأ 404 أو 503 ننتقل تلقائياً للنموذج التالي
                    continue

                # معالجة التدفق وإرسال الأحداث للعميل
                for line in res.iter_lines():
                    if not line:
                        continue
                    line_str = line.decode("utf-8")
                    if line_str.startswith("data: "):
                        data_content = line_str[6:].strip()
                        try:
                            parsed_data = json.loads(data_content)
                            candidates = parsed_data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                for p in parts:
                                    text_chunk = p.get("text", "")
                                    if text_chunk:
                                        stream_success = True
                                        event_data = json.dumps({"chunk": text_chunk, "model": model}, ensure_ascii=False)
                                        yield f"data: {event_data}\n\n"
                        except Exception:
                            continue

                if stream_success:
                    yield "data: [DONE]\n\n"
                    return

            except Exception as e:
                model_errors.append(f"{model}: {str(e)}")
                continue

        # في حال فشلت كل النماذج
        error_msg = "تعذر الحصول على استجابة من نماذج الذكاء الاصطناعي حالياً. يرجى التحقق من الاتصال أو المحاولة لاحقاً."
        yield f"data: {json.dumps({'error': error_msg}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate_events()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

@app.route("/chat", methods=["POST"])
def chat_legacy():
    """مسار تقليدي احتياطي (Non-streaming Fallback)"""
    client_ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(client_ip)
    if not allowed:
        return jsonify({
            "error": f"تم تجاوز الحد المسموح للطلبات المؤقتة (Rate Limit). يرجى الانتظار {retry_after} ثانية."
        }), 429, {"Retry-After": str(retry_after)}

    api_key = get_api_key()
    if not api_key:
        return jsonify({"error": "مفتاح Gemini API غير مهيأ."}), 500

    validated, err_msg = validate_and_sanitize_payload(request.json or {})
    if err_msg:
        return jsonify({"error": err_msg}), 400

    question, history = validated
    contents = history + [{"role": "user", "parts": [{"text": question}]}]

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION_TEXT}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096}
    }

    errors = []
    for model in PRIMARY_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            res = requests.post(
                url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=25
            )
            if res.ok:
                parts = res.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                answer = "\n".join(p["text"] for p in parts if "text" in p).strip()
                if answer:
                    return jsonify({"answer": answer, "model": model, "status": "success"})
            err_msg = res.json().get("error", {}).get("message", res.text[:200])
            errors.append(f"{model}: {err_msg}")
        except Exception as e:
            errors.append(f"{model}: {str(e)}")

    return jsonify({"error": "تعذر توليد الإجابة من النماذج.", "details": errors}), 503

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 Amro AI CHAT يعمل الآن بنجاح على المنفذ: {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
