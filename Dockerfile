FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
VOLUME ["/data"]
EXPOSE 6767
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6767/health', timeout=4)"
CMD ["uv", "run", "--no-dev", "python", "-m", "memoratum"]
