FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard.py dashboard.html history.html ./

ENV LOG_DIR=/app/data
EXPOSE 8080

CMD ["python", "dashboard.py"]
