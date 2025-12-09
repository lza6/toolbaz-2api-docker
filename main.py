import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends, HTTPException, Header
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.providers.toolbaz_provider import ToolbazProvider

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("toolbaz-api")

provider = ToolbazProvider()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"启动 {settings.APP_NAME}...")
    await provider.initialize()
    yield
    logger.info("正在关闭浏览器资源...")
    await provider.close()

app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")

async def verify_key(authorization: str = Header(None)):
    if settings.API_MASTER_KEY and settings.API_MASTER_KEY != "1":
        if not authorization or authorization != f"Bearer {settings.API_MASTER_KEY}":
            raise HTTPException(status_code=401, detail="Invalid API Key")

@app.post("/v1/chat/completions", dependencies=[Depends(verify_key)])
async def chat_completions(request: Request):
    try:
        data = await request.json()
        return await provider.chat_completion(data)
    except Exception as e:
        logger.error(f"Error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/v1/models")
async def list_models():
    return await provider.get_models()

@app.get("/", response_class=HTMLResponse)
async def root():
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Toolbaz-2API Running. (static/index.html not found)"