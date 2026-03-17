FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Create data directories
RUN mkdir -p /app/attachments /app/static

# Environment
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Run with gunicorn (production WSGI server)
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 --timeout 120 --access-logfile - --error-logfile - --preload"]
