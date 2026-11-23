# Build from the project root:  docker build -f docker/Dockerfile -t ai-meter .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ai_meter/cli.py aimeter.py
COPY src/ai_meter/dashboard.html src/ai_meter/config.example.yaml src/ai_meter/config.starter.yaml src/ai_meter/mark.svg ./
COPY docker/entrypoint.sh .
RUN chmod +x entrypoint.sh
EXPOSE 8787
ENTRYPOINT ["/app/entrypoint.sh"]
