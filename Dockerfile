FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# --with-deps installs Chromium + all system libraries it needs in one step
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8080

CMD ["python", "app.py"]
