import os
import re
import json
import time
import html
import threading
from collections import defaultdict
from flask import Flask, jsonify, render_template_string, request, Response, stream_with_context
import requests

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
    def __init__(self, requests_per_minute=35, burst_limit=10, burst_window=10):
        self.rpm = requests_per_minute
        self.burst_limit = burst_limit
        self.burst_window = burst_window
        self.requests = defaultdict(list)
        self.lock = threading.Lock()

    def is_allowed(self, ip: str):
        now = time.time()
        with self.lock:
            self.requests[ip] = [t for t in self.requests[ip] if now - t < 60]

            burst_reqs = [t for t in self.requests[ip] if now - t < self.burst_window]
            if len(burst_reqs) >= self.burst_limit:
                retry_after = int(self.burst_window - (now - burst_reqs[0])) + 1
                return False, max(1, retry_after)

            if len(self.requests[ip]) >= self.rpm:
                retry_after = int(60 - (now - self.requests[ip][0])) + 1
                return False, max(1, retry_after)

            self.requests[ip].append(now)
            return True, 0

rate_limiter = ThreadSafeRateLimiter(requests_per_minute=35, burst_limit=10, burst_window=10)

def get_client_ip():
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    if request.headers.get("X-Real-IP"):
        return request.headers.get("X-Real-IP").strip()
    return request.remote_addr or "127.0.0.1"

def get_api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    invalid_keys = ["", "ضع_مفتاح_Gemini_API_هنا", "your_new_api_key_here"]
    if not key or key in invalid_keys:
        return None
    return key

# ==========================================
# 2. نماذج الذكاء الاصطناعي وتعليمات النظام
# ==========================================
PRIMARY_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.8-flash"
]

# تعليمات نظام واضحة ومباشرة: التركيز التام على إجابة السؤال دون الترويج للمطور إلا عند السؤال الصريح عنه
SYSTEM_INSTRUCTION_TEXT = (
    "أنت Amro AI ✨، مساعد ذكاء اصطناعي عربي عملي ومفيد وفائق الذكاء. "
    "مهمتك الأساسية: أجب عن سؤال المستخدم مباشرة وبكل دقة ووضوح ولباقة، وقدّم حلولاً برمجية ومعلومات منسقة باحترافية. "
    "توجيه هام وصارم: لا تذكر مطورك ولا تتحدث عنه أو عن إنجازاته أو سيرته الذاتية في سياق المحادثات العادية أو البرمجية إطلاقاً. "
    "الحالة الوحيدة المسموح فيها بذكره: إذا سألك المستخدم صراحةً وبشكل مباشر: (من أنت؟ أو من طورك؟ أو من صنعك؟)، حينها فقط أجب بإيجاز واعتدال أنك Amro AI الذي طوّره المهندس عمرو الشرافي مع رابط https://amrocv.vercel.app . في غير ذلك، ركز بنسبة 100% على موضوع السؤال المطلوب فقط. "
    "نظّم ردودك بتنسيق Markdown أنيق ومرتب بدون إطالة أو حشو غير ضروري."
)

# ==========================================
# 3. التحقق من المدخلات وتطهيرها
# ==========================================
def validate_and_sanitize_payload(data):
    if not isinstance(data, dict):
        return None, "صيغة الطلب غير صالحة."

    question = data.get("question", "")
    if not isinstance(question, str):
        return None, "نص السؤال غير صالح."

    question = question.strip()
    if not question:
        return None, "يرجى كتابة سؤال قبل الإرسال."

    if len(question) > 5000:
        return None, "نص السؤال طويل جداً (الحد الأقصى 5000 حرف)."

    raw_history = data.get("history", [])
    if not isinstance(raw_history, list):
        raw_history = []

    clean_history = []
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
        clean_history.append({
            "role": role,
            "parts": [{"text": text[:4000]}]
        })

    return (question, clean_history), None

# ==========================================
# 4. الواجهة الأمامية (Desktop & Mobile Optimized)
# ==========================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Amro AI CHAT ✨ | المساعد الذكي</title>
  
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800;900&display=swap" rel="stylesheet">
  
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>

  <style>
    :root {
      --primary: #1d4ed8;
      --primary-hover: #1e40af;
      --primary-light: #eff6ff;
      --primary-border: #bfdbfe;
      --bg-main: #f8fafc;
      --bg-card: #ffffff;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --border-color: #e2e8f0;
      --sidebar-width: 290px;
      --radius-lg: 14px;
      --radius-md: 10px;
      --radius-sm: 6px;
    }

    * {
      box-sizing: border-box;
      font-family: 'Cairo', -apple-system, BlinkMacSystemFont, sans-serif;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
      scrollbar-width: thin;
      scrollbar-color: #cbd5e1 transparent;
    }

    /* تخصيص شريط التمرير بالكامل وإزالة أسهم ويندوز المزعجة */
    ::-webkit-scrollbar {
      width: 5px;
      height: 5px;
    }
    ::-webkit-scrollbar-track {
      background: transparent;
    }
    ::-webkit-scrollbar-thumb {
      background: #cbd5e1;
      border-radius: 8px;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: #94a3b8;
    }
    ::-webkit-scrollbar-button {
      display: none !important;
      width: 0 !important;
      height: 0 !important;
    }

    html, body {
      height: 100%;
      height: 100vh;
      height: 100dvh;
      width: 100%;
      background: var(--bg-main);
      color: var(--text-main);
      overflow: hidden;
      display: flex;
      font-size: 13.5px;
    }

    /* ============================================================
       الشريط الجانبي للكمبيوتر: يتمرر بكامل عناصره لراحة العرض
    ============================================================ */
    .sidebar {
      width: var(--sidebar-width);
      height: 100%;
      height: 100vh;
      height: 100dvh;
      background: var(--bg-card);
      border-left: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      z-index: 100;
      transition: width 0.24s cubic-bezier(0.4, 0, 0.2, 1);
      /* تمرير القائمة الجانبية بكامل محتوياتها بسلاسة */
      overflow-y: auto;
      overflow-x: hidden;
    }

    .sidebar.collapsed {
      width: 0 !important;
      border-left: none !important;
      visibility: hidden;
      pointer-events: none;
    }

    .sidebar-header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: #f8fafc;
      padding: 11px 14px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-shrink: 0;
    }

    .sidebar-header h2 {
      font-size: 13.5px;
      font-weight: 800;
      color: #0f172a;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .sidebar-header h2 .badge {
      background: #e2e8f0;
      color: #475569;
      font-size: 10px;
      padding: 1px 6px;
      border-radius: 10px;
      font-weight: 700;
    }

    .close-sidebar-btn {
      display: none;
      background: none;
      border: none;
      font-size: 16px;
      color: var(--text-muted);
      cursor: pointer;
      padding: 3px;
      border-radius: 5px;
    }
    .close-sidebar-btn:hover { background: #e2e8f0; color: #0f172a; }

    .sidebar-actions {
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      border-bottom: 1px solid var(--border-color);
      background: #ffffff;
      flex-shrink: 0;
    }

    .sidebar-dev-btn {
      display: flex;
      align-items: center;
      gap: 8px;
      width: 100%;
      padding: 7px 10px;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: all 0.15s ease;
      text-align: right;
    }
    .sidebar-dev-btn:hover {
      background: #dbeafe;
      border-color: #93c5fd;
    }
    .sidebar-dev-btn .dev-code-icon {
      background: #1d4ed8;
      color: #ffffff;
      font-family: monospace;
      font-weight: 800;
      font-size: 11.5px;
      padding: 3px 6px;
      border-radius: 5px;
    }
    .sidebar-dev-btn .dev-btn-text {
      flex: 1;
      display: flex;
      flex-direction: column;
    }
    .sidebar-dev-btn .dev-main-title {
      font-size: 11.5px;
      font-weight: 800;
      color: #1e3a8a;
    }
    .sidebar-dev-btn .dev-sub-title {
      font-size: 10px;
      color: #2563eb;
      font-weight: 600;
    }

    .new-chat-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      width: 100%;
      padding: 7px 10px;
      background: linear-gradient(135deg, #1d4ed8, #2563eb);
      color: #ffffff;
      border: none;
      border-radius: var(--radius-sm);
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.15s ease;
      box-shadow: 0 1px 4px rgba(37,99,235,0.2);
    }
    .new-chat-btn:hover { background: linear-gradient(135deg, #1e40af, #1d4ed8); }

    .search-box {
      position: relative;
      width: 100%;
    }
    .search-box input {
      width: 100%;
      padding: 6px 28px 6px 8px;
      font-size: 11.5px;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-sm);
      background: #f8fafc;
      color: var(--text-main);
      outline: none;
    }
    .search-box input:focus {
      border-color: var(--primary);
      background: #ffffff;
    }
    .search-box .search-icon {
      position: absolute;
      right: 9px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 11px;
      color: var(--text-muted);
      pointer-events: none;
    }

    /* منطقة عرض بطاقات الملاحظات بكامل السعة */
    .notes-list {
      padding: 8px 10px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      flex: 1;
    }

    .empty-notes {
      text-align: center;
      color: var(--text-muted);
      font-size: 11.5px;
      margin: 24px 8px;
      line-height: 1.6;
    }

    .note-card {
      background: #f8fafc;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-sm);
      padding: 7px 9px;
      cursor: pointer;
      transition: all 0.15s ease;
      display: flex;
      flex-direction: column;
      gap: 4px;
      flex-shrink: 0;
    }
    .note-card:hover {
      border-color: #93c5fd;
      background: #eff6ff;
    }
    .note-card.active {
      border-color: var(--primary);
      background: #e0f2fe;
    }
    .note-card.pinned {
      border-right: 3px solid #f59e0b;
      background: #fffbeb;
    }

    .note-card-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 6px;
    }
    .note-title {
      font-size: 11.5px;
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
      gap: 2px;
      opacity: 0.7;
    }
    .note-card:hover .note-card-actions { opacity: 1; }
    
    .note-action-btn {
      background: none;
      border: none;
      color: #64748b;
      cursor: pointer;
      font-size: 10px;
      padding: 2px 3px;
      border-radius: 3px;
    }
    .note-action-btn:hover { background: #e2e8f0; color: #0f172a; }
    .note-action-btn.delete-btn:hover { background: #fee2e2; color: #dc2626; }
    .note-action-btn.pin-btn.is-pinned { color: #d97706; }

    .note-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 9.5px;
      color: var(--text-muted);
    }

    .sidebar-footer {
      position: sticky;
      bottom: 0;
      z-index: 10;
      background: #f8fafc;
      padding: 8px 12px;
      border-top: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 10.5px;
      color: var(--text-muted);
      flex-shrink: 0;
    }
    .clear-all-notes-btn {
      background: none;
      border: none;
      color: #dc2626;
      font-size: 10.5px;
      cursor: pointer;
      font-weight: 600;
      padding: 2px 4px;
      border-radius: 4px;
    }
    .clear-all-notes-btn:hover { background: #fee2e2; }

    /* ============================================================
       منطقة المحادثة الرئيسية بتنسيق خطوط مدمج ومريح للكمبيوتر
    ============================================================ */
    .main-wrapper {
      flex: 1 1 0%;
      min-width: 0;
      height: 100%;
      height: 100vh;
      height: 100dvh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      position: relative;
    }

    header {
      background: #ffffff;
      border-bottom: 1px solid var(--border-color);
      padding: 7px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      box-shadow: 0 1px 3px rgba(0,0,0,0.02);
      z-index: 10;
      flex-shrink: 0;
      height: 48px;
    }

    .header-right, .header-left {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .menu-toggle-btn {
      background: #f1f5f9;
      color: #1e293b;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-sm);
      padding: 5px 10px;
      font-size: 11.5px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .menu-toggle-btn:hover { background: #e2e8f0; }

    .brand-title h1 {
      font-size: 15px;
      font-weight: 800;
      color: #1e3a8a;
    }

    .status-badge {
      font-size: 11px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 14px;
      background: #f1f5f9;
      color: #475569;
      border: 1px solid #e2e8f0;
      display: flex;
      align-items: center;
      gap: 4px;
      white-space: nowrap;
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

    .chat-container {
      flex: 1 1 0%;
      min-height: 0;
      max-width: 860px;
      width: 100%;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      padding: 12px 18px;
      overflow: hidden;
    }

    .chat-messages {
      flex: 1 1 0%;
      min-height: 0;
      overflow-y: auto;
      overflow-x: hidden;
      padding: 4px 6px 16px 6px;
      display: flex;
      flex-direction: column;
      gap: 13px;
      scroll-behavior: smooth;
    }

    .msg-wrapper {
      display: flex;
      flex-direction: column;
      max-width: 82%;
      animation: fadeIn 0.2s ease-out;
    }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(3px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .msg-wrapper.user { align-self: flex-start; }
    .msg-wrapper.bot {
      align-self: flex-end;
      width: 100%;
      max-width: 92%;
    }

    .msg-bubble {
      padding: 10px 15px;
      border-radius: var(--radius-md);
      font-size: 13.5px;
      line-height: 1.68;
      position: relative;
      word-break: break-word;
      overflow-wrap: break-word;
    }

    .msg-wrapper.user .msg-bubble {
      background: linear-gradient(135deg, #1d4ed8, #2563eb);
      color: #ffffff;
      border-bottom-left-radius: 3px;
      box-shadow: 0 1px 6px rgba(37,99,235,0.16);
      font-weight: 600;
    }

    .msg-wrapper.bot .msg-bubble {
      background: #ffffff;
      color: #1e293b;
      border: 1px solid #e2e8f0;
      border-bottom-right-radius: 3px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.02);
    }

    .welcome-card {
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: var(--radius-md);
      padding: 13px 16px;
      line-height: 1.7;
      box-shadow: 0 1px 4px rgba(0,0,0,0.02);
    }
    .welcome-card h3 {
      font-size: 14.5px;
      color: #1e3a8a;
      margin-bottom: 5px;
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .welcome-card p {
      font-size: 13px;
      color: #334155;
      margin-bottom: 5px;
    }
    .welcome-card .welcome-hint {
      font-size: 11.5px;
      color: #64748b;
      margin-top: 6px;
      padding-top: 6px;
      border-top: 1px dashed #e2e8f0;
    }

    .typing-box {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 3px 0;
      color: #1d4ed8;
      font-weight: 700;
      font-size: 13px;
    }
    .typing-dots { display: inline-flex; gap: 3px; }
    .typing-dots span {
      width: 4px;
      height: 4px;
      background: #2563eb;
      border-radius: 50%;
      animation: bounce 1.2s infinite ease-in-out;
    }
    .typing-dots span:nth-child(1) { animation-delay: 0s; }
    .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
    .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes bounce {
      0%, 80%, 100% { transform: translateY(0); opacity: 0.4; }
      40% { transform: translateY(-4px); opacity: 1; }
    }

    .streaming-cursor {
      display: inline-block;
      width: 6px;
      height: 14px;
      background: #1d4ed8;
      animation: blink 0.8s infinite;
      border-radius: 1px;
    }
    @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

    .msg-content { line-height: 1.68; font-size: 13.5px; }
    .msg-content p { margin-bottom: 6px; }
    .msg-content p:last-child { margin-bottom: 0; }
    .msg-content h1, .msg-content h2, .msg-content h3 { color: #0f172a; margin: 10px 0 5px 0; font-weight: 800; }
    .msg-content h1 { font-size: 15px; }
    .msg-content h2 { font-size: 14px; }
    .msg-content h3 { font-size: 13.5px; }
    .msg-content ul, .msg-content ol { margin: 5px 16px 8px; }
    .msg-content li { margin-bottom: 3px; }
    .msg-content blockquote {
      border-right: 3px solid var(--primary);
      padding: 3px 10px;
      background: #f8fafc;
      color: #475569;
      margin: 6px 0;
      border-radius: 0 5px 5px 0;
    }
    .msg-content table {
      border-collapse: collapse;
      width: 100%;
      margin: 8px 0;
      font-size: 12.5px;
      background: #fff;
    }
    .msg-content th, .msg-content td {
      border: 1px solid #e2e8f0;
      padding: 5px 8px;
      text-align: right;
    }
    .msg-content th { background: #f1f5f9; font-weight: 700; }

    .code-container {
      position: relative;
      margin: 8px 0;
      border-radius: 7px;
      overflow: hidden;
      background: #282c34;
      border: 1px solid #3e4451;
      direction: ltr;
      text-align: left;
    }
    .code-header {
      background: #21252b;
      padding: 4px 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      font-family: monospace;
      font-size: 10.5px;
      color: #abb2bf;
    }
    .code-copy-btn {
      background: rgba(255,255,255,0.1);
      border: 1px solid rgba(255,255,255,0.2);
      color: #e5e7eb;
      font-size: 10px;
      padding: 2px 6px;
      border-radius: 4px;
      cursor: pointer;
      font-family: 'Cairo', sans-serif;
    }
    .code-copy-btn:hover { background: rgba(255,255,255,0.2); color: #fff; }
    .code-copy-btn.copied { background: #166534; color: #bbf7d0; border-color: #22c55e; }

    pre code.hljs {
      padding: 8px 10px;
      font-family: 'Consolas', 'Courier New', monospace;
      font-size: 12.5px;
      line-height: 1.5;
      overflow-x: auto;
    }

    .msg-toolbar {
      display: flex;
      align-items: center;
      gap: 5px;
      margin-top: 5px;
      padding: 0 2px;
    }
    .msg-btn {
      background: #f1f5f9;
      border: 1px solid #e2e8f0;
      color: #475569;
      font-size: 10.5px;
      padding: 2px 7px;
      border-radius: 5px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 3px;
      font-weight: 600;
    }
    .msg-btn:hover { background: #e2e8f0; color: #0f172a; }
    .msg-btn.copied { background: #dcfce7; color: #15803d; border-color: #86efac; }

    /* حقل الإدخال المدمج والخفيف */
    .input-section {
      background: #ffffff;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-md);
      padding: 6px 10px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.05);
      display: flex;
      align-items: flex-end;
      gap: 8px;
      flex-shrink: 0;
    }

    textarea#questionInput {
      flex: 1;
      padding: 6px 8px;
      border: none;
      outline: none;
      resize: none;
      font-size: 13.5px;
      max-height: 110px;
      min-height: 38px;
      line-height: 1.5;
      color: var(--text-main);
      background: transparent;
    }

    .action-controls {
      display: flex;
      align-items: center;
      gap: 6px;
      padding-bottom: 2px;
    }

    .btn-send {
      background: linear-gradient(135deg, #1d4ed8, #2563eb);
      color: white;
      border: none;
      border-radius: 8px;
      padding: 7px 15px;
      font-size: 12.5px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 5px;
      box-shadow: 0 1px 4px rgba(37,99,235,0.2);
      white-space: nowrap;
    }
    .btn-send:hover { background: linear-gradient(135deg, #1e40af, #1d4ed8); }

    .btn-stop {
      background: #dc2626;
      color: white;
      border: none;
      border-radius: 8px;
      padding: 7px 15px;
      font-size: 12.5px;
      font-weight: 700;
      cursor: pointer;
      display: none;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
    }
    .btn-stop:hover { background: #b91c1c; }

    .backdrop-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.4);
      backdrop-filter: blur(2px);
      z-index: 95;
    }

    /* نافذة المطور المنبثقة */
    .modal-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(3px);
      z-index: 200;
      align-items: center;
      justify-content: center;
      padding: 14px;
    }
    .modal-overlay.active { display: flex; }

    .dev-modal {
      background: #ffffff;
      width: 100%;
      max-width: 420px;
      border-radius: var(--radius-md);
      box-shadow: 0 10px 25px rgba(0,0,0,0.18);
      overflow: hidden;
      animation: modalPop 0.2s ease-out;
      border: 1px solid var(--border-color);
    }
    @keyframes modalPop {
      from { transform: scale(0.94); opacity: 0; }
      to { transform: scale(1); opacity: 1; }
    }

    .dev-modal-header {
      background: linear-gradient(135deg, #1e3a8a, #2563eb);
      color: white;
      padding: 18px 16px;
      text-align: center;
      position: relative;
    }
    .dev-modal-header .code-badge-large {
      font-family: monospace;
      font-size: 18px;
      font-weight: 900;
      background: #ffffff;
      color: #1d4ed8;
      width: 48px;
      height: 48px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 6px;
      box-shadow: 0 2px 6px rgba(0,0,0,0.12);
    }
    .dev-modal-header h3 { font-size: 16.5px; font-weight: 800; }
    .dev-modal-header p { font-size: 12px; opacity: 0.9; margin-top: 2px; }
    .modal-close-btn {
      position: absolute;
      top: 10px;
      left: 10px;
      background: rgba(255,255,255,0.2);
      border: none;
      color: white;
      width: 26px;
      height: 26px;
      border-radius: 50%;
      cursor: pointer;
      font-size: 13px;
    }

    .dev-modal-body {
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      font-size: 12.5px;
      line-height: 1.65;
      color: #334155;
    }
    .skill-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 3px;
    }
    .skill-tag {
      background: #eff6ff;
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
      padding: 2px 7px;
      border-radius: 8px;
      font-size: 11px;
      font-weight: 700;
    }
    .dev-modal-footer {
      padding: 11px 16px;
      background: #f8fafc;
      border-top: 1px solid var(--border-color);
      display: flex;
      justify-content: center;
    }
    .cv-btn-large {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: #1d4ed8;
      color: white;
      text-decoration: none;
      padding: 8px 18px;
      border-radius: var(--radius-sm);
      font-size: 12.5px;
      font-weight: 700;
    }
    .cv-btn-large:hover { background: #1e40af; }

    /* ============================================================
       قواعد التجاوب المخصصة للهواتف
    ============================================================ */
    @media (max-width: 768px) {
      .sidebar {
        position: fixed;
        top: 0;
        bottom: 0;
        right: 0;
        width: 82%;
        max-width: 300px;
        transform: translateX(100%);
        box-shadow: -4px 0 20px rgba(0,0,0,0.15);
      }
      .sidebar.open { transform: translateX(0); }
      .close-sidebar-btn { display: inline-block; }
      .backdrop-overlay.show { display: block; }

      header {
        padding: 7px 12px;
        height: 46px;
      }
      .brand-title h1 { font-size: 14.5px; }
      .menu-toggle-btn { padding: 4px 8px; font-size: 11.5px; }
      .status-badge { padding: 3px 7px; font-size: 10.5px; }

      .chat-container { padding: 8px; }
      .msg-wrapper { max-width: 94%; }
      .msg-wrapper.bot { max-width: 98%; }
      .msg-bubble { padding: 9px 12px; font-size: 13px; }

      .input-section { padding: 5px 8px; }
      textarea#questionInput { font-size: 13px; min-height: 38px; }
      .btn-send, .btn-stop { padding: 7px 12px; font-size: 12px; }
    }
  </style>
</head>
<body>

  <div class="backdrop-overlay" id="backdropOverlay" onclick="closeSidebarMobile()"></div>

  <!-- الشريط الجانبي (دفتر الملاحظات) -->
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <h2>
        <span>📒 دفتر الملاحظات</span>
        <span class="badge" id="notesCountBadge">0</span>
      </h2>
      <button class="close-sidebar-btn" onclick="closeSidebarMobile()">✕</button>
    </div>

    <div class="sidebar-actions">
      <!-- زر المطور مع أيقونة </> -->
      <button class="sidebar-dev-btn" onclick="openDevModal()">
        <span class="dev-code-icon">&lt;/&gt;</span>
        <div class="dev-btn-text">
          <span class="dev-main-title">المطور (عمرو الشرافي)</span>
          <span class="dev-sub-title">السيرة الذاتية ومعرض الأعمال ↗</span>
        </div>
      </button>

      <button class="new-chat-btn" onclick="createNewChat()">
        <span>➕</span>
        <span>محادثة جديدة</span>
      </button>

      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input type="text" id="notesSearchInput" placeholder="بحث في الملاحظات..." oninput="handleNotesSearch()">
      </div>
    </div>

    <div class="notes-list" id="notesListContainer"></div>

    <div class="sidebar-footer">
      <span id="savedStatsText">لا توجد محادثات</span>
      <button class="clear-all-notes-btn" onclick="confirmClearAllNotes()">مسح الكل</button>
    </div>
  </aside>

  <!-- الواجهة الرئيسية للمحادثة -->
  <div class="main-wrapper">
    <header>
      <div class="header-right">
        <button class="menu-toggle-btn" onclick="handleSidebarToggle()">
          <span>📒</span>
          <span>الملاحظات</span>
        </button>
        <div class="brand-title">
          <h1>Amro AI CHAT ✨</h1>
        </div>
      </div>

      <div class="header-left">
        <div class="status-badge ready" id="statusBadge">جاهز ✨</div>
      </div>
    </header>

    <div class="chat-container">
      <div class="chat-messages" id="chatMessagesArea"></div>

      <div class="input-section">
        <textarea 
          id="questionInput" 
          placeholder="اكتب سؤالك هنا... 💬"
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
    </div>
  </div>

  <!-- نافذة المطور المنبثقة -->
  <div class="modal-overlay" id="devModalOverlay" onclick="closeDevModalOnBg(event)">
    <div class="dev-modal">
      <div class="dev-modal-header">
        <button class="modal-close-btn" onclick="closeDevModal()">✕</button>
        <div class="code-badge-large">&lt;/&gt;</div>
        <h3>عمرو الشرافي | Amro Alshurafy</h3>
        <p>Software Engineer & AI Solutions Developer</p>
      </div>
      <div class="dev-modal-body">
        <p>
          مرحباً بك! أنا <strong>عمرو الشرافي</strong>، مطور برمجيات وتطبيقات وويب وحلول ذكاء اصطناعي.
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
          </div>
        </div>
      </div>
      <div class="dev-modal-footer">
        <a class="cv-btn-large" href="https://amrocv.vercel.app" target="_blank" rel="noopener noreferrer">
          <span>🌐 فتح السيرة الذاتية ومعرض الأعمال</span>
          <span>↗</span>
        </a>
      </div>
    </div>
  </div>

  <script>
    marked.setOptions({ gfm: true, breaks: true });

    const STORAGE_KEYS = {
      NOTES: "amro_ai_notes_v2",
      ACTIVE_CHAT: "amro_ai_active_chat_v2"
    };

    let currentHistory = [];
    let currentNoteId = null;
    let isGenerating = false;
    let abortController = null;
    let notes = loadSavedNotes();
    let searchTerm = "";

    function loadSavedNotes() {
      try {
        const raw = localStorage.getItem(STORAGE_KEYS.NOTES);
        return raw ? JSON.parse(raw) : [];
      } catch (e) {
        return [];
      }
    }

    function saveNotesToStorage() {
      try {
        localStorage.setItem(STORAGE_KEYS.NOTES, JSON.stringify(notes));
      } catch (e) {}
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
      } catch (e) {}
    }

    function handleSidebarToggle() {
      const sidebar = document.getElementById("sidebar");
      const overlay = document.getElementById("backdropOverlay");

      if (window.innerWidth <= 768) {
        const isOpen = sidebar.classList.contains("open");
        if (isOpen) {
          sidebar.classList.remove("open");
          overlay.classList.remove("show");
        } else {
          sidebar.classList.add("open");
          overlay.classList.add("show");
        }
      } else {
        sidebar.classList.toggle("collapsed");
      }
    }

    function closeSidebarMobile() {
      const sidebar = document.getElementById("sidebar");
      const overlay = document.getElementById("backdropOverlay");
      sidebar.classList.remove("open");
      overlay.classList.remove("show");
    }

    function openDevModal() {
      document.getElementById("devModalOverlay").classList.add("active");
    }
    function closeDevModal() {
      document.getElementById("devModalOverlay").classList.remove("active");
    }
    function closeDevModalOnBg(e) {
      if (e.target.id === "devModalOverlay") closeDevModal();
    }

    function setStatus(text, type = "ready") {
      const badge = document.getElementById("statusBadge");
      badge.textContent = text;
      badge.className = "status-badge " + type;
    }

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
        btnElement.innerHTML = successMsg;
        setTimeout(() => { btnElement.innerHTML = origText; }, 1800);
      });
    }

    function renderMarkdownToHtml(markdownText) {
      const rawHtml = marked.parse(markdownText || "");
      return DOMPurify.sanitize(rawHtml, { ADD_ATTR: ['target', 'rel'] });
    }

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

      if (role === "bot") {
        const toolbar = document.createElement("div");
        toolbar.className = "msg-toolbar";

        const copyBtn = document.createElement("button");
        copyBtn.className = "msg-btn";
        copyBtn.innerHTML = "<span>📋</span><span>نسخ</span>";
        copyBtn.onclick = () => copyText(wrapper.dataset.fullText || "", copyBtn);

        toolbar.appendChild(copyBtn);
        bubble.appendChild(toolbar);
      }

      wrapper.appendChild(bubble);
      wrapper.dataset.fullText = text;
      area.appendChild(wrapper);

      if (role === "bot" && text) {
        enhanceCodeBlocks(wrapper);
      }

      scrollToBottom();
      return { wrapper, bubble, content };
    }

    function enhanceCodeBlocks(container) {
      container.querySelectorAll("pre code").forEach((codeEl) => {
        hljs.highlightElement(codeEl);

        const pre = codeEl.parentElement;
        if (pre.parentElement.classList.contains("code-container")) return;

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

    function scrollToBottom() {
      const area = document.getElementById("chatMessagesArea");
      area.scrollTop = area.scrollHeight;
    }

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

      filtered.sort((a, b) => {
        if (a.pinned && !b.pinned) return -1;
        if (!a.pinned && b.pinned) return 1;
        return (b.updatedAt || b.id) - (a.updatedAt || a.id);
      });

      document.getElementById("notesCountBadge").textContent = notes.length;
      document.getElementById("savedStatsText").textContent = 
        notes.length ? `${notes.length} محادثة محفوظة` : "لا توجد محادثات";

      if (filtered.length === 0) {
        container.innerHTML = `
          <div class="empty-notes">
            ${searchTerm.trim() ? "لا توجد نتائج مطابقة 🔍" : "دفتر الملاحظات فارغ 📭<br>ابدأ بمحادثة وستُحفظ هنا تلقائياً."}
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
              <button class="note-action-btn pin-btn ${note.pinned ? 'is-pinned' : ''}" title="${note.pinned ? 'إلغاء التثبيت' : 'تثبيت'}" onclick="togglePinNote(event, ${note.id})">📌</button>
              <button class="note-action-btn" title="إعادة تسمية" onclick="renameNotePrompt(event, ${note.id})">✏️</button>
              <button class="note-action-btn" title="تصدير" onclick="exportNoteMenu(event, ${note.id})">📥</button>
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

    function autoSaveCurrentChatToNotes() {
      if (!currentHistory || currentHistory.length === 0) return;

      const firstUserMsg = currentHistory.find(m => m.role === "user")?.parts?.[0]?.text || "محادثة جديدة";
      const cleanTitle = firstUserMsg.length > 28 ? firstUserMsg.substring(0, 28) + "..." : firstUserMsg;
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

      setStatus("📒 معروضة", "ready");
      if (window.innerWidth <= 768) closeSidebarMobile();
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

      const format = prompt("اختر صيغة التصدير:\n1 = ملف Markdown (.md)\n2 = ملف نصي (.txt)\n3 = طباعة", "1");
      if (!format) return;

      let content = `# ${n.title}\nتاريخ: ${n.date}\nمساعد الذكاء الاصطناعي: Amro AI\n\n---\n\n`;
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
      if (!confirm("هل تريد حذف هذه المحادثة من دفتر الملاحظات؟")) return;

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
      if (confirm("⚠️ هل أنت متأكد من مسح كافة المحادثات المحفوظة؟")) {
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

      if (window.innerWidth <= 768) closeSidebarMobile();
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
      scrollToBottom();
    }

    function showWelcomeMessage() {
      const area = document.getElementById("chatMessagesArea");
      area.innerHTML = `
        <div class="welcome-card">
          <h3><span>✨</span><span>أهلاً بك في Amro AI CHAT</span></h3>
          <p>أنا مساعدك الذكي، جاهز للإجابة عن أسئلتك، كتابة الأكواد، وتنسيق الأفكار بكل سرعة ودقة.</p>
          <div class="welcome-hint">💬 تفضل بكتابة استفسارك في الأسفل للبدء مباشرة.</div>
        </div>
      `;
    }

    async function handleSendMessage() {
      if (isGenerating) return;

      const input = document.getElementById("questionInput");
      const question = input.value.trim();
      if (!question) return;

      createMessageElement("user", question);
      input.value = "";
      adjustTextareaHeight(input);

      setGeneratingUI(true);
      setStatus("يكتب الآن ✍️", "streaming");

      const botElements = createMessageElement("bot", "");
      botElements.content.innerHTML = `
        <div class="typing-box">
          <span>جاري الكتابة... ✍️</span>
          <span class="typing-dots"><span></span><span></span><span></span></span>
          <span class="streaming-cursor"></span>
        </div>
      `;

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

        if (response.status === 429) {
          const errData = await response.json();
          throw new Error(errData.error || "تم تجاوز حد الطلبات المؤقت (Rate Limit)، يرجى الانتظار قليلاً.");
        }

        if (!response.ok) {
          const errData = await response.json().catch(() => ({}));
          throw new Error(errData.error || `خطأ في الاتصال (${response.status})`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data: ")) continue;
            const dataStr = trimmed.slice(6).trim();

            if (dataStr === "[DONE]") break;

            try {
              const parsed = JSON.parse(dataStr);
              if (parsed.error) throw new Error(parsed.error);
              if (parsed.chunk) {
                fullAnswer += parsed.chunk;
              }
            } catch (jsonErr) {}
          }
        }

        botElements.content.innerHTML = renderMarkdownToHtml(fullAnswer);
        botElements.wrapper.dataset.fullText = fullAnswer;
        enhanceCodeBlocks(botElements.wrapper);

        currentHistory.push({ role: "user", parts: [{ text: question }] });
        currentHistory.push({ role: "model", parts: [{ text: fullAnswer }] });

        autoSaveCurrentChatToNotes();
        setStatus(currentNoteId ? "📒 محفوظة" : "جاهز ✨", "ready");

      } catch (err) {
        if (err.name === "AbortError") {
          if (fullAnswer) {
            botElements.content.innerHTML = renderMarkdownToHtml(fullAnswer) + `<div style="font-size: 11px; color: #dc2626; margin-top: 5px;">⏹️ (تم إيقاف الرد)</div>`;
            botElements.wrapper.dataset.fullText = fullAnswer;
            enhanceCodeBlocks(botElements.wrapper);
            currentHistory.push({ role: "user", parts: [{ text: question }] });
            currentHistory.push({ role: "model", parts: [{ text: fullAnswer }] });
            autoSaveCurrentChatToNotes();
          } else {
            botElements.content.innerHTML = `<span style="color: #64748b; font-size: 11.5px;">⏹️ تم إيقاف توليد الإجابة.</span>`;
          }
          setStatus("متوقف ⏹️", "ready");
        } else {
          botElements.content.innerHTML = `<span style="color: #dc2626; font-weight: 700;">⚠️ عذراً: ${escapeHtml(err.message)}</span>`;
          setStatus("خطأ ❌", "ready");
        }
      } finally {
        abortController = null;
        setGeneratingUI(false);
        input.focus();
        scrollToBottom();
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

    const textarea = document.getElementById("questionInput");
    function adjustTextareaHeight(el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 110) + "px";
    }
    textarea.addEventListener("input", function() { adjustTextareaHeight(this); });

    textarea.addEventListener("keydown", function(e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSendMessage();
      }
    });

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
            setStatus(currentNoteId ? "📒 محفوظة" : "جاهز ✨", "ready");
            return;
          }
        }
      } catch (e) {}

      showWelcomeMessage();
    }

    window.addEventListener("DOMContentLoaded", initApplication);
  </script>
</body>
</html>
"""

# ==========================================
# 5. مسارات الـ Backend
# ==========================================

@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response

@app.route("/")
@app.route("/api")
@app.route("/api/index.py")
def home():
    if request.args.get("debug"):
        return jsonify({
            "path": request.path,
            "environ": {k: str(v) for k, v in request.environ.items() if "PATH" in k or "URL" in k or "VERCEL" in k or "MATCH" in k or "ROUTE" in k or "HTTP" in k}
        })
    return render_template_string(HTML_TEMPLATE)

@app.route("/debug", methods=["GET", "POST"])
@app.route("/api/debug", methods=["GET", "POST"])
def debug():
    return jsonify({
        "path": request.path,
        "full_path": request.full_path,
        "method": request.method,
        "environ_path_info": request.environ.get("PATH_INFO"),
        "environ_matched_path": request.environ.get("HTTP_X_MATCHED_PATH"),
        "headers": dict(request.headers)
    })

@app.route("/chat/stream", methods=["POST"])
@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    client_ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(client_ip)
    if not allowed:
        return jsonify({
            "error": f"تم تجاوز الحد المسموح للطلبات المؤقتة (Rate Limit). يرجى الانتظار {retry_after} ثانية."
        }), 429, {"Retry-After": str(retry_after)}

    api_key = get_api_key()
    if not api_key:
        return jsonify({
            "error": "مفتاح Gemini API غير مضبوط في ملف .env."
        }), 500

    validated, err_msg = validate_and_sanitize_payload(request.json or {})
    if err_msg:
        return jsonify({"error": err_msg}), 400

    question, history = validated
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
                        err_text = res.json().get("error", {}).get("message", res.text[:200])
                    except Exception:
                        err_text = res.text[:200]
                    model_errors.append(f"{model}: {err_text}")
                    continue

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

        error_msg = "تعذر الحصول على استجابة من نماذج الذكاء الاصطناعي حالياً."
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
@app.route("/api/chat", methods=["POST"])
def chat_legacy():
    client_ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(client_ip)
    if not allowed:
        return jsonify({
            "error": f"تم تجاوز الحد المسموح للطلبات المؤقتة. يرجى الانتظار {retry_after} ثانية."
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
