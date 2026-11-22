# 屏幕问答云端：纯标准库 Python 镜像，1C1G 就能跑
FROM python:3.12-slim
WORKDIR /app
COPY store.py app.py ./
COPY static ./static
# 可选：想让网页版支持"上传题图"，取消下一行注释（镜像会大 ~250MB）
# RUN pip install --no-cache-dir rapidocr-onnxruntime
ENV PORT=8080
VOLUME ["/app/data"]
ENV SQA_DB=/app/data/sqa.db
EXPOSE 8080
CMD ["python", "app.py", "--port", "8080"]
