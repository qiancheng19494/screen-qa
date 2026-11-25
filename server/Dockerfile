# 屏幕问答云端：纯标准库 Python 镜像，1C1G 就能跑
FROM python:3.12-slim
WORKDIR /app
COPY *.py ./
COPY static ./static
# 网页版"上传题图"需要 OCR 引擎（镜像约 +250MB）
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir rapidocr-onnxruntime
ENV PORT=8080
VOLUME ["/app/data"]
ENV SQA_DB=/app/data/sqa.db
EXPOSE 8080
CMD ["python", "app.py", "--port", "8080"]
