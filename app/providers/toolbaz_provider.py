import json
import time
import uuid
import asyncio
import random
import re
import html
from typing import Dict, Any, Optional, List
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from playwright.async_api import async_playwright, Page, BrowserContext, Error as PlaywrightError
from loguru import logger
import httpx

from app.core.config import settings
from app.utils.sse_utils import create_sse_data, create_chat_completion_chunk, DONE_CHUNK

# --- 单个工作单元 (Worker) ---
class BrowserWorker:
    """代表一个独立的浏览器无痕窗口"""
    def __init__(self, browser):
        self.browser = browser
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.uses_count = 0
        self.created_at = 0
        self.id = str(uuid.uuid4())[:8]

    async def init(self):
        """初始化这个窗口"""
        try:
            if self.context:
                await self.close()

            # 创建无痕上下文
            self.context = await self.browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                java_script_enabled=True,
                bypass_csp=True,
                ignore_https_errors=True
            )
            
            self.page = await self.context.new_page()
            # 屏蔽 webdriver 特征
            await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            
            # 预热 (带重试机制)
            logger.info(f"🔧 [Worker-{self.id}] 正在预热...")
            
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # 随机延迟
                    await asyncio.sleep(random.uniform(1, 2))
                    
                    await self.page.goto(
                        "https://toolbaz.com/writer/chat-gpt-alternative", 
                        wait_until="domcontentloaded", 
                        timeout=45000
                    )
                    break 
                except PlaywrightError as e:
                    if "ERR_CONNECTION_CLOSED" in str(e) or "Timeout" in str(e):
                        logger.warning(f"⚠️ [Worker-{self.id}] 预热失败 (尝试 {attempt+1}/{max_retries}): {e}")
                        if attempt == max_retries - 1:
                            raise e 
                        await asyncio.sleep(5) 
                    else:
                        raise e

            # 稍微动一下鼠标
            try:
                await self.page.mouse.move(random.randint(100, 500), random.randint(100, 500))
            except: pass
            
            self.created_at = time.time()
            self.uses_count = 0
            logger.info(f"✅ [Worker-{self.id}] 就绪")
            return True
        except Exception as e:
            logger.error(f"❌ [Worker-{self.id}] 初始化失败: {e}")
            await self.close()
            return False

    async def get_token_data(self):
        """在这个特定窗口中获取 Token"""
        if not self.page or self.page.is_closed():
            success = await self.init()
            if not success:
                return {"error": "Worker re-init failed"}

        try:
            await self.page.wait_for_function("typeof window.xA1pY === 'function' || typeof xA1pY === 'function'", timeout=5000)
        except:
            try:
                logger.warning(f"⚠️ [Worker-{self.id}] 函数未就绪，尝试刷新页面...")
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
            except Exception as e:
                return {"error": f"Reload failed: {str(e)}"}

        result = await self.page.evaluate("""() => {
            try {
                function getCookie(name) {
                    const value = `; ${document.cookie}`;
                    const parts = value.split(`; ${name}=`);
                    if (parts.length === 2) return parts.pop().split(';').shift();
                    return null;
                }
                let sessionId = getCookie("SessionID");
                if (!sessionId) {
                    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
                    sessionId = "";
                    for (let i = 0; i < 36; i++) sessionId += chars.charAt(Math.floor(Math.random() * chars.length));
                    document.cookie = `SessionID=${sessionId}; path=/`;
                }
                
                let token = "";
                if (typeof window.xA1pY === 'function') token = window.xA1pY();
                else if (typeof xA1pY === 'function') token = xA1pY();
                else return { error: "xA1pY missing" };

                return { sessionId, token };
            } catch (e) { return { error: e.toString() }; }
        }""")
        
        self.uses_count += 1
        return result

    async def close(self):
        try:
            if self.context: await self.context.close()
        except: pass
        self.context = None
        self.page = None

# --- 核心提供者 (Provider) ---
class ToolbazProvider:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.pool = asyncio.Queue()
        self.api_token_url = "https://data.toolbaz.com/token.php"
        self.api_writing_url = "https://data.toolbaz.com/writing.php"
        
        # 🔥 限流器变量
        self.request_timestamps: List[float] = []
        self.rate_limit_lock = asyncio.Lock()

    async def initialize(self):
        """启动浏览器并创建池子"""
        logger.info(f"🚀 正在启动浏览器集群 (并发数: {settings.BROWSER_POOL_SIZE})...")
        self.playwright = await async_playwright().start()
        
        launch_args = [
            "--no-sandbox", 
            "--disable-setuid-sandbox", 
            "--disable-dev-shm-usage", 
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled"
        ]
        
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=launch_args
        )

        for i in range(settings.BROWSER_POOL_SIZE):
            worker = BrowserWorker(self.browser)
            asyncio.create_task(self._init_and_push_worker(worker))
            await asyncio.sleep(3)
        
        logger.info(f"✅ 浏览器池启动指令已下发...")

    async def _init_and_push_worker(self, worker: BrowserWorker):
        success = await worker.init()
        if success:
            await self.pool.put(worker)
        else:
            logger.warning(f"⚠️ Worker-{worker.id} 初始化失败，10秒后重试...")
            await asyncio.sleep(10)
            await self._init_and_push_worker(worker)

    async def _wait_for_rate_limit(self):
        """🔥 核心限流逻辑：确保每分钟不超过5次请求"""
        async with self.rate_limit_lock:
            current_time = time.time()
            # 清理超过60秒的旧记录
            self.request_timestamps = [t for t in self.request_timestamps if current_time - t < 60]
            
            # 限制为每分钟 5 次 (留1次余量，设为4次比较安全)
            MAX_REQUESTS_PER_MINUTE = 4 
            
            if len(self.request_timestamps) >= MAX_REQUESTS_PER_MINUTE:
                # 计算需要等待的时间
                oldest_request = self.request_timestamps[0]
                wait_time = 60 - (current_time - oldest_request) + 1
                if wait_time > 0:
                    logger.warning(f"🚦 触发速率限制 (5req/min)，正在排队等待 {wait_time:.2f} 秒...")
                    await asyncio.sleep(wait_time)
            
            # 记录这次请求的时间
            self.request_timestamps.append(time.time())

    def _clean_response_text(self, text: str) -> str:
        if not text: return ""
        text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        text = html.unescape(text)
        text = re.sub(r'^\[model:.*?\]\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^Toolbaz.*?:', '', text, flags=re.IGNORECASE)
        return text.strip()

    async def chat_completion(self, request_data: Dict[str, Any]):
        model = request_data.get("model", settings.DEFAULT_MODEL)
        messages = request_data.get("messages", [])
        stream = request_data.get("stream", True)
        
        last_user_content = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "Hello")
        padding = "\u3164"
        formatted_text = f"{padding} : {last_user_content}{padding}"

        # 1. 获取 Worker
        logger.info(f"⏳ 正在等待空闲浏览器窗口 (当前可用: {self.pool.qsize()})...")
        worker: BrowserWorker = await self.pool.get()
        
        try:
            logger.info(f"🤖 使用窗口 [Worker-{worker.id}] 处理请求...")
            
            if worker.uses_count > settings.CONTEXT_MAX_USES:
                logger.info(f"♻️ 窗口 [Worker-{worker.id}] 使用次数过多，正在重建...")
                await worker.init()

            # 2. 获取凭证
            security_data = await worker.get_token_data()
            if security_data.get("error"):
                logger.error(f"❌ [Worker-{worker.id}] Token获取失败: {security_data.get('error')}")
                await worker.init()
                security_data = await worker.get_token_data()
                if security_data.get("error"):
                    raise Exception(f"Token生成失败: {security_data['error']}")

            session_id = security_data["sessionId"]
            payload_token = security_data["token"]

            # 🔥 3. 在发送请求前，执行限流检查
            await self._wait_for_rate_limit()

            # 4. 发送 HTTP 请求
            async with httpx.AsyncClient() as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Origin": "https://toolbaz.com",
                    "Referer": "https://toolbaz.com/writer/chat-gpt-alternative",
                    "X-Requested-With": "XMLHttpRequest",
                    "Cookie": f"SessionID={session_id}"
                }

                token_resp = await client.post(
                    self.api_token_url,
                    data={"session_id": session_id, "token": payload_token},
                    headers=headers,
                    timeout=20
                )
                
                if token_resp.status_code != 200:
                    raise ValueError(f"Token API 状态码错误: {token_resp.status_code}")
                
                token_json = token_resp.json()
                if not token_json.get("success"):
                    raise ValueError(f"Token API 拒绝: {token_json}")
                
                capcha_token = token_json["token"]

                chat_resp = await client.post(
                    self.api_writing_url,
                    data={
                        "text": formatted_text,
                        "capcha": capcha_token,
                        "model": model,
                        "session_id": session_id
                    },
                    headers=headers,
                    timeout=120
                )
                
                # 🔥 专门捕获 400 Quota Limit 错误
                if chat_resp.status_code == 400 and "quota limit" in chat_resp.text:
                    logger.warning("⚠️ 触发 API 硬性限流，返回 429 给客户端")
                    # 归还 worker，因为 worker 本身没问题，是 IP 没额度了
                    await self.pool.put(worker)
                    return JSONResponse({"error": "Rate limit exceeded (5 req/min). Please wait."}, status_code=429)

                if chat_resp.status_code != 200:
                    raise ValueError(f"Writing API 错误: {chat_resp.status_code} - {chat_resp.text[:100]}")
                
                clean_text = self._clean_response_text(chat_resp.text)
                request_id = f"chatcmpl-{uuid.uuid4()}"

                # 5. 返回结果
                if not stream:
                    await self.pool.put(worker)
                    logger.info(f"🔙 窗口 [Worker-{worker.id}] 已归还")
                    return JSONResponse({
                        "id": request_id,
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": clean_text}, "finish_reason": "stop"}]
                    })

                async def stream_generator():
                    try:
                        chunk_size = 20
                        for i in range(0, len(clean_text), chunk_size):
                            part = clean_text[i:i+chunk_size]
                            yield create_sse_data(create_chat_completion_chunk(request_id, model, part))
                            await asyncio.sleep(0.02)
                        yield create_sse_data(create_chat_completion_chunk(request_id, model, "", "stop"))
                        yield DONE_CHUNK
                    finally:
                        await self.pool.put(worker)
                        logger.info(f"🔙 窗口 [Worker-{worker.id}] 已归还 (流结束)")

                return StreamingResponse(stream_generator(), media_type="text/event-stream")

        except Exception as e:
            logger.error(f"❌ [Worker-{worker.id}] 处理严重错误: {e}")
            asyncio.create_task(self._recycle_worker(worker))
            raise HTTPException(status_code=500, detail=str(e))

    async def _recycle_worker(self, worker: BrowserWorker):
        """后台回收并重置 Worker"""
        logger.info(f"🔧 [Worker-{worker.id}] 正在后台重置...")
        await asyncio.sleep(5)
        success = await worker.init()
        if success:
            await self.pool.put(worker)
            logger.info(f"✅ [Worker-{worker.id}] 重置成功并归还池子")
        else:
            logger.error(f"💀 [Worker-{worker.id}] 重置失败，尝试再次重置...")
            await asyncio.sleep(10)
            await self._recycle_worker(worker)

    async def get_models(self):
        return JSONResponse({
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": int(time.time()), "owned_by": "toolbaz"}
                for m in settings.MODELS
            ]
        })

    async def close(self):
        while not self.pool.empty():
            worker = await self.pool.get()
            await worker.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()