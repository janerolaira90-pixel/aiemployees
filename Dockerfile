FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY eunaria ./eunaria
COPY config ./config
RUN useradd --create-home eunaria && mkdir -p /app/data && chown eunaria /app/data
USER eunaria
CMD ["python", "-m", "eunaria", "run"]
