FROM python:3.12-slim

WORKDIR /app

# Install system dependencies needed by Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget curl \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Chromium (must run after pip install playwright)
RUN playwright install chromium

COPY . .

EXPOSE 8080

CMD ["python", "app.py"]
