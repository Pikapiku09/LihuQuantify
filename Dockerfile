# LihuQuantify 镜像（第四轮清单4/5：DS918+ 部署）
# scheduler 与 web 共用同一镜像，compose 里用不同 command 区分。
FROM python:3.12-slim

# 时区纪律（第四轮清单5）：loguru 时间戳 / date.today() 跟随系统时区
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层（pyproject.toml 不变则命中构建缓存）。
# 只装第三方依赖（tomllib 提取），不 pip install 项目本身——
# 运行靠 PYTHONPATH=/app/src（入口脚本均有 sys.path 兜底）。
COPY pyproject.toml README.md ./
RUN python -c "import tomllib; deps = tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; deps += ['fastapi>=0.110.0', 'uvicorn>=0.29.0']; print('\n'.join(deps))" > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt \
    && rm /tmp/req.txt

# 代码层（data/outputs 不进镜像——通过卷挂载持久化，见 docker-compose.yml）
COPY src/ ./src/
COPY web/ ./web/
COPY config/ ./config/
COPY run_scheduler.py run_backtest.py ./

# 运行时路径锚点：scheduler.py 的 _ROOT 解析为 /app（data/outputs 挂载点）
# HEALTHCHECK（P2-9-11）：web 服务健康探活（/api/health 已存在）；scheduler 容器
# 无 web，如需自愈请在其上叠加 compose healthcheck 或关闭。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["python", "run_scheduler.py", "--mode", "paper"]
