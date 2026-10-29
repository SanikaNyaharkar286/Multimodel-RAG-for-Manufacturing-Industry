
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["sh", "-c", "exec functions-framework --source=cloud_function.py --target=process_gcs_pdf --signature-type=cloudevent --host=0.0.0.0 --port=${PORT:-8080}"]
