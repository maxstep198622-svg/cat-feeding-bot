FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY catbot ./catbot
COPY rules.html ./
ENV CATBOT_CONFIG=/app/config.toml \
    PYTHONUNBUFFERED=1
CMD ["python", "-m", "catbot"]
