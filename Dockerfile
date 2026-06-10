FROM python:3.11-slim

# Install system dependencies and Chromium / ChromeDriver
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Set default environment variables
ENV HEADLESS=true
ENV PORT=8080

# Run the Telegram bot
CMD ["python", "main.py"]
