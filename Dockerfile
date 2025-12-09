# 使用官方 Python 镜像
FROM python:3.10-slim

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
# 设置 Playwright 浏览器路径
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖 (使用清华源)
RUN pip install --no-cache-dir --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 安装 Playwright 浏览器 (Chromium)
RUN playwright install chromium --with-deps

# 复制应用代码
COPY . .

# 创建非 root 用户 (安全最佳实践)
RUN useradd --create-home appuser && \
    chown -R appuser:appuser /app && \
    chown -R appuser:appuser /ms-playwright

USER appuser

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]