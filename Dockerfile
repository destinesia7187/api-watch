FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
RUN useradd --create-home --uid 10001 apiwatch && mkdir -p var/mail && chown -R apiwatch:apiwatch /app
USER apiwatch
EXPOSE 8000
CMD ["python", "-m", "api_watch", "web", "--host", "0.0.0.0"]
