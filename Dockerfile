FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system bot && useradd --system --gid bot --home-dir /app bot

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=bot:bot bot.py ./
COPY --chown=bot:bot cogs ./cogs

USER bot
CMD ["python", "bot.py"]
