FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.0 httpx==0.28.1 boto3==1.37.38 pydantic==2.11.3 pydantic-settings==2.8.1 prometheus-client==0.21.1 python-multipart==0.0.20 psycopg2-binary==2.9.9 temporalio==1.10.0 numpy==2.1.3 opencv-python-headless==4.11.0.86 pillow==11.1.0 pypdfium2==5.8.0 pytesseract==0.3.13
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
WORKDIR /app
