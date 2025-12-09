from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    # 这一行告诉 Pydantic 自动去读取 .env 文件
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "toolbaz-2api"
    APP_VERSION: str = "3.1.0 (Full-Models)"
    
    # 如果 .env 里没配 API_MASTER_KEY，默认就是 "1"
    API_MASTER_KEY: str = "1"
    
    # 🔥 完整模型列表 (恢复了之前的所有模型)
    MODELS: List[str] = [
        "toolbaz-v4.5-fast",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "claude-sonnet-4",
        "gpt-5",
        "grok-4-fast"
    ]
    DEFAULT_MODEL: str = "toolbaz-v4.5-fast"

    # 🔥 并发配置 (这里是默认值) 🔥
    # 这里的 1 是为了防止你忘记配置 .env 时程序报错。
    # 只要你在 .env 里写了 BROWSER_POOL_SIZE=5，这里的值就会被覆盖为 5。
    BROWSER_POOL_SIZE: int = 1
    
    # 默认每个窗口用 50 次就重置
    CONTEXT_MAX_USES: int = 50 

settings = Settings()