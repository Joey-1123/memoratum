FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
VOLUME ["/data"]
EXPOSE 6767
CMD ["uv", "run", "--no-dev", "python", "-m", "memoratum"]
