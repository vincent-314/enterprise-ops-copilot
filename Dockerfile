FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY knowledge_base ./knowledge_base
COPY knowledge_base_en ./knowledge_base_en
COPY scripts ./scripts

RUN uv sync --no-dev --extra postgres

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "enterprise_ops_agent.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
