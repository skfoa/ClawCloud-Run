"""
ClawCloud 自动登录及多区域巡逻脚本 (最终校准版)
- 支持区域：美东 (us-east-1) 和 日本 (ap-northeast-1)
- 优化了巡逻逻辑与 Cookie 保存机制
"""

import base64
import os
import random
import re
import sys
import time
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

# ==================== 配置 ====================
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()

# 设置默认入口为美东区域
LOGIN_ENTRY_URL = "https://us-east-1.run.claw.cloud/login"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30 
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))


class Telegram:
    def __init__(self):
        self.token = os.environ.get('TG_BOT_TOKEN')
        self.chat_id = os.environ.get('TG_CHAT_ID')
        self.ok = bool(self.token and self.chat_id)
    
    def send(self, msg):
        if not self.ok: return
        try: requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                         data={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"}, timeout=30)
        except: pass
    
    def photo(self, path, caption=""):
        if not self.ok or not os.path.exists(path): return
        try:
            with open(path, 'rb') as f:
                requests.post(f"https://api.telegram.org/bot{self.token}/sendPhoto",
                            data={"chat_id": self.chat_id, "caption": caption[:1024]},
                            files={"photo": f}, timeout=60)
        except: pass
    
    def flush_updates(self):
        if not self.ok: return 0
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates", params={"timeout": 0}, timeout=10)
            data = r.json()
            if data.get("ok") and data.get("result"): return data["result"][-1]["update_id"] + 1
        except: pass
        return 0
    
    def wait_code(self, timeout=120):
        if not self.ok: return None
        offset = self.flush_updates()
        deadline = time.time() + timeout
        pattern = re.compile(r"^/code\s+(\d{6,8})$")
        while time.time() < deadline:
            try:
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                               params={"timeout": 20, "offset": offset}, timeout=30)
                data = r.json()
                if data.get("ok"):
                    for upd in data.get("result", []):
                        offset = upd["update_id"] + 1
                        msg = upd.get("message") or {}
                        if str(msg.get("chat", {}).get("id")) == str(self.chat_id):
                            text = (msg.get("text") or "").strip()
                            m = pattern.match(text)
                            if m: return m.group(1)
            except: pass
            time.sleep(2)
        return None


class SecretUpdater:
    def __init__(self):
        self.token = os.environ.get('REPO_TOKEN')
        self.repo = os.environ.get('GITHUB_REPOSITORY')
        self.ok = bool(self.token and self.repo)
    
    def update(self, name, value):
        if not self.ok: return False
        try:
            from nacl import encoding, public
            headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github.v3+json"}
            r = requests.get(f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key",
                           headers=headers, timeout=30)
            if r.status_code != 200: return False
            key_data = r.json()
            pk = public.PublicKey(key_data['key'].encode(), encoding.Base64Encoder())
            encrypted = public.SealedBox(pk).encrypt(value.encode())
            r = requests.put(f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                           headers=headers,
                           json={"encrypted_value": base64.b64encode(encrypted).decode(), "key_id": key_data['key_id']},
                           timeout=30)
            return r.status_code in [201, 204]
        except: return False


class AutoLogin:
    def __init__(self):
        self.username = os.environ.get('GH_USERNAME')
        self.password = os.environ.get('GH_PASSWORD')
        self.gh_session = os.environ.get('GH_SESSION', '').strip()
        self.tg = Telegram()
        self.secret = SecretUpdater()
        self.shots = []
        self.logs = []
        self.n = 0

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line); self.logs.append(line)

    def shot(self, page, name):
        self.n += 1
        f = f"{self.n:02d}_{name}.png"
        try: page.screenshot(path=f); self.shots.append(f)
        except: pass
        return f

    def click(self, page, sels, desc=""):
        for s in sels:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=3000):
                    time.sleep(random.uniform(0.5, 1.0))
                    el.click(); self.log(f"已点击: {desc}", "SUCCESS")
                    return True
            except: pass
        return False

    def save_cookie(self, value):
        if not value: return
        self.log(f"提取新 Cookie 成功", "SUCCESS")
        if self.secret.update('GH_SESSION', value):
            self.log("已自动更新 GitHub Secrets 中的 GH_SESSION", "SUCCESS")
        else:
            self.log("Secrets 更新失败，尝试发送至 Telegram", "WARN")
            self.tg.send(f"🔑 <b>新 Cookie (手动更新备用)</b>:\n<code>{value}</code>")

    def login_github(self, page):
        self.log("正在执行 GitHub 身份认证...", "STEP")
        page.locator('input[name="login"]').fill(self.username)
        page.locator('input[name="password"]').fill(self.password)
        page.locator('input[type="submit"]').first.click()
        time.sleep(5)
        
        if 'verified-device' in page.url or 'device-verification' in page.url:
            self.log("检测到设备验证，请批准邮箱/App链接", "WARN")
            self.tg.send("⚠️ <b>需要设备验证</b>\n请在 30 秒内批准登录。")
            time.sleep(DEVICE_VERIFY_WAIT)
        
        if 'two-factor' in page.url:
            if 'two-factor/mobile' in page.url:
                self.log("需要 Mobile 验证，请在手机 GitHub App 确认数字", "WARN")
                shot = self.shot(page, "2FA_Mobile")
                self.tg.send("⚠️ <b>需要手机批准</b>\n请看下图中的数字并批准。")
                self.tg.photo(shot, "2FA 数字截图")
                time.sleep(TWO_FACTOR_WAIT)
            else:
                self.log("需要 TOTP 验证码", "WARN")
                self.tg.send("🔐 <b>需要验证码</b>\n请发送：<code>/code 123456</code>")
                code = self.tg.wait_code(timeout=TWO_FACTOR_WAIT)
                if code:
                    page.locator('input[autocomplete="one-time-code"]').fill(code)
                    page.keyboard.press("Enter")
                    time.sleep(5)
        return "github.com/login" not in page.url

    def keepalive(self, page):
        """定向巡逻美东和日本资源区"""
        self.log("🚀 开始执行多区域资源保活巡逻...", "STEP")
        regions = [("us-east-1", "美东 (US-East)"), ("ap-northeast-1", "日本 (Japan)")]
        for rid, rname in regions:
            url = f"https://{rid}.run.claw.cloud/apps"
            try:
                self.log(f"巡逻区域 {rname}...", "INFO")
                page.goto(url, timeout=45000)
                page.wait_for_load_state('networkidle', timeout=20000)
                self.log(f"✅ {rname} 资源列表已触发加载", "SUCCESS")
                time.sleep(3)
            except Exception as e:
                self.log(f"⚠️ {rname} 访问延迟或异常: {str(e)[:40]}", "WARN")
        self.shot(page, "巡逻报告")

    def run(self):
        self.log(f"开始任务，用户: {self.username}")
        if not self.username or not self.password: sys.exit(1)
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-blink-features=AutomationControlled'])
            context = browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
            
            if self.gh_session:
                context.add_cookies([{'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'}])

            page = context.new_page()
            try:
                page.goto(SIGNIN_URL, timeout=60000)
                self.click(page, ['button:has-text("GitHub")', '[data-provider="github"]'], "GitHub 按钮")
                time.sleep(5)
                
                if 'github.com/login' in page.url:
                    if not self.login_github(page): 
                        self.log("GitHub 登录最终失败", "ERROR"); sys.exit(1)
                elif 'github.com/login/oauth/authorize' in page.url:
                    self.click(page, ['button[name="authorize"]'], "OAuth 授权")
                
                # 等待重定向回到 Claw 控制台
                success = False
                for _ in range(40):
                    if 'claw.cloud' in page.url and 'signin' not in page.url:
                        success = True; break
                    time.sleep(1)
                
                if success:
                    self.keepalive(page)
                    # 尝试保存最新的 Cookie
                    for c in context.cookies():
                        if c['name'] == 'user_session' and 'github' in c['domain']:
                            self.save_cookie(c['value']); break
                    self.tg.send("✅ <b>ClawCloud 多区域巡逻任务已完成</b>\n状态：账号活跃，美东与日本资源已巡检。")
                else:
                    self.log("重定向回控制台超时", "ERROR")
                    self.tg.send("❌ <b>ClawCloud 巡逻失败</b>\n原因：登录后未能进入控制台。")

            except Exception as e:
                self.log(f"任务崩溃: {e}", "ERROR")
                self.tg.send(f"❌ <b>脚本运行异常</b>\n错误详情请查看 GitHub Action 日志。")
            finally:
                browser.close()

if __name__ == "__main__":
    AutoLogin().run()
