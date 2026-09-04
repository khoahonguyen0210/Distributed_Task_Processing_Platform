FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip install . && useradd --create-home --uid 10001 taskq

USER taskq
EXPOSE 8000

CMD ["python", "-m", "app.main"]
