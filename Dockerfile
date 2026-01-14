FROM python:3.10-slim

ENV APP_HOME=/app
WORKDIR $APP_HOME

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

EXPOSE 8080

RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

CMD exec gunicorn --bind :8080 --workers 1 --threads 8 --timeout 0 app:app
