FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# --with-deps installs Chromium + all system libraries it needs in one step
RUN playwright install --with-deps chromium

COPY . .

ENV PORT=8080 DB_PATH=/app/data/leads.db
RUN mkdir -p /app/data
EXPOSE 8080

CMD ["python", "app.py"]
