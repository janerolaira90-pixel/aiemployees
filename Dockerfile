FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY eunaria ./eunaria
COPY config ./config
# Task database lives in /app/data. Attach persistent storage there
# (a Railway Volume with mount path /app/data, or the compose.yaml bind mount).
RUN mkdir -p /app/data
CMD ["python", "-m", "eunaria", "run"]
