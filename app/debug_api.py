"""
Отладочный скрипт для проверки используемых API ключей
"""
import os
from dotenv import load_dotenv
import sys

def check_api_keys():
    # Проверяем системные переменные ДО загрузки .env
    raw_api_key = os.environ.get('BINANCE_API_KEY')
    raw_api_secret = os.environ.get('BINANCE_API_SECRET')
    
    def mask_key(key):
        if not key:
            return "не установлен"
        return f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "слишком короткий ключ"
    
    print("\n--- Системные переменные ДО загрузки .env ---")
    print(f"BINANCE_API_KEY: {mask_key(raw_api_key)}")
    print(f"BINANCE_API_SECRET: {mask_key(raw_api_secret)}")
    
    # Загружаем переменные из .env файла (если он существует)
    dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    print(f"Путь к .env файлу: {dotenv_path}")
    print(f"Файл существует: {os.path.exists(dotenv_path)}")
    
    # Загрузка .env
    load_dotenv(dotenv_path)
    
    # Получаем API ключи из окружения ПОСЛЕ загрузки .env
    api_key = os.environ.get('BINANCE_API_KEY')
    api_secret = os.environ.get('BINANCE_API_SECRET')
    
    # Маскируем ключи для безопасности
    def mask_key(key):
        if not key:
            return "не установлен"
        return f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "слишком короткий ключ"
    
    # Выводим информацию
    print("\n--- Информация об API ключах ---")
    print(f"API ключ: {mask_key(api_key)}")
    print(f"API секрет: {mask_key(api_secret)}")
    
    # Проверяем наличие системных переменных окружения
    print("\n--- Источники API ключей ---")
    # Системные переменные
    system_api_key = os.environ.get('BINANCE_API_KEY')
    if system_api_key:
        print(f"Системная переменная BINANCE_API_KEY: {mask_key(system_api_key)}")
    else:
        print("Системная переменная BINANCE_API_KEY не установлена")
    
    # Проверяем .env файл напрямую
    try:
        with open(dotenv_path, 'r') as f:
            env_content = f.read()
            for line in env_content.splitlines():
                if line.startswith('BINANCE_API_KEY='):
                    env_api_key = line.split('=', 1)[1]
                    print(f"API ключ из .env файла: {mask_key(env_api_key)}")
                    
                    if env_api_key != api_key:
                        print("ВНИМАНИЕ: API ключ в .env файле отличается от загруженного!")
                    else:
                        print("API ключ в .env файле соответствует загруженному.")
    except Exception as e:
        print(f"Ошибка при чтении .env файла: {e}")

if __name__ == "__main__":
    check_api_keys()
