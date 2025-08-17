FROM python:3.10-slim

WORKDIR /app

# Установка необходимых пакетов
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Копирование и установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода приложения
COPY app/ ./

# Определение переменной среды для уведомления Python о том, что мы в продакшн
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Настройка прав доступа
RUN chmod +x /app/bot.py

# Открытие порта для API
EXPOSE 5000

# Запуск бота
CMD ["python", "bot.py"]
