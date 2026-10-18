FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir python-telegram-bot==21.10 playwright==1.48.0 python-dotenv==1.0.1

COPY . .

CMD ["python", "bot.py"]
