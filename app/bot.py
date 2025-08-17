# bot.py - Простой спот-бот для переключения между активами по комплексной стратегии
# Поддерживает стратегии:
# 1. SIMPLE_MA: MA7 > MA25 = держим коин, MA7 < MA25 = держим USDT
# 2. MA_RSI_ATR: Мульти-таймфреймовый анализ с использованием MA, RSI и ATR
import os
import json
import time
import math
import threading
from datetime import datetime, timezone
from typing import Tuple, Optional, Dict, Any, List
from flask import Flask, jsonify
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException, BinanceOrderException

# Импорт индикаторов (если установлены)
try:
    import pandas as pd
    import numpy as np
    import ta
except ImportError:
    pass

# ========== Утилиты логов ==========
def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

try:
    # Импортируем нашу индикаторную систему
    from indicators import IndicatorBasedStrategy
except ImportError:
    log("❌ Не удалось импортировать модуль indicators.py, будет использован только базовый метод MA", "WARN")

# ========== Управление конфигурацией ==========
from dataclasses import dataclass
from typing import List

@dataclass
class ConfigurationStatus:
    api_keys_present: bool
    api_keys_valid: bool
    environment_source: str  # "system", "file", "default"
    configuration_issues: List[str]
    safety_checks_passed: bool

@dataclass
class TradingModeStatus:
    current_mode: str  # "LIVE"
    mode_source: str   # откуда взято значение
    blocking_issues: List[str]
    last_mode_change: Optional[datetime]

class EnvironmentConfig:
    """Централизованное управление переменными окружения"""
    
    def __init__(self):
        self.config_status = None
        self.load_environment()
        self.validate_configuration()
    
    def load_environment(self):
        """Загрузка переменных окружения с правильным приоритетом"""
        log("🚀 НАЧАЛО ЗАГРУЗКИ КОНФИГУРАЦИИ", "CONFIG")
        log("=" * 60, "CONFIG")
        
        # Загружаем переменные из .env файла
        # Определяем путь к .env файлу относительно текущего файла
        current_dir = os.path.dirname(os.path.abspath(__file__))
        env_file_path = os.path.join(current_dir, '.env')
        
        log(f"🔍 Поиск .env файла: {env_file_path}", "CONFIG")
        if os.path.exists(env_file_path):
            log("✅ .env файл найден, загружаем...", "CONFIG")
            load_dotenv(env_file_path)
        else:
            log("⚠️ .env файл не найден, используем системные переменные", "CONFIG")
            load_dotenv()  # Попытка загрузить из текущей директории
        
        # Загружаем все переменные с логированием
        self.api_key = self._get_env_with_logging("BINANCE_API_KEY", "").strip() or None
        self.api_secret = self._get_env_with_logging("BINANCE_API_SECRET", "").strip() or None
        self.symbol = self._get_env_with_logging("SYMBOL", "BNBUSDT", str.upper)
        self.interval = self._get_env_with_logging("INTERVAL", "30m")
        self.ma_short = self._get_env_with_logging("MA_SHORT", "7", int)
        self.ma_long = self._get_env_with_logging("MA_LONG", "25", int)
        # Тестовый режим удален в пользу работы напрямую с биржей
        
        # Остальные параметры
        self.check_interval = self._get_env_with_logging("CHECK_INTERVAL", "60", int)
        self.state_path = self._get_env_with_logging("STATE_PATH", "state.json")
        self.ma_spread_bps = self._get_env_with_logging("MA_SPREAD_BPS", "0.5", float)
        self.max_retries = self._get_env_with_logging("MAX_RETRIES", "3", int)
        self.health_check_interval = self._get_env_with_logging("HEALTH_CHECK_INTERVAL", "300", int)
        self.min_balance_usdt = self._get_env_with_logging("MIN_BALANCE_USDT", "10.0", float)
        # Staking related
        self.enable_staking = self._get_env_with_logging("ENABLE_STAKING", "false").lower() == "true"
        self.stake_percent = self._get_env_with_logging("STAKE_PERCENT", "0.8", float)
        self.stake_mode = self._get_env_with_logging("STAKE_MODE", "flexible")
        self.min_stake_usdt = self._get_env_with_logging("MIN_STAKE_USDT", "1.0", float)
        self.stake_all = self._get_env_with_logging("STAKE_ALL", "false").lower() == "true"

        log("✅ КОНФИГУРАЦИЯ ЗАГРУЖЕНА УСПЕШНО", "CONFIG")
        log("=" * 60, "CONFIG")
    
    def _get_env_with_logging(self, name: str, default: str, convert_func=None):
        """Получение переменной окружения с подробным логированием"""
        # Проверяем системные переменные окружения
        system_value = os.environ.get(name)
        
        # Получаем значение из os.getenv (уже загружено через load_dotenv)
        dotenv_value = os.getenv(name)
        
        # Определяем источник и финальное значение
        if system_value is not None:
            final_value = system_value
            source = "системные переменные окружения"
        elif dotenv_value is not None:
            final_value = dotenv_value
            source = ".env файл"
        else:
            final_value = default
            source = "значение по умолчанию"
        
        # Применяем конвертацию если нужно
        if convert_func:
            try:
                converted_value = convert_func(final_value)
                self._log_env_var(name, converted_value, convert_func(default) if default else None, source)
                return converted_value
            except (ValueError, TypeError) as e:
                log(f"❌ Ошибка конвертации {name}={final_value}: {e}. Используется значение по умолчанию.", "ERROR")
                converted_default = convert_func(default) if default else None
                self._log_env_var(name, converted_default, converted_default, "значение по умолчанию (ошибка конвертации)")
                return converted_default
        else:
            self._log_env_var(name, final_value, default, source)
            return final_value
    
    def _log_env_var(self, name: str, value: Any, default: Any, source: str = "unknown") -> None:
        """Логирование переменной окружения с источником"""
        if value == default:
            log(f"🔧 ENV {name}={value} (источник: значение по умолчанию)", "CONFIG")
        else:
            log(f"🔧 ENV {name}={value} (источник: {source})", "CONFIG")
    
    def validate_configuration(self):
        """Валидация критических настроек"""
        issues = []
        
        # Проверяем API ключи
        api_keys_present = bool(self.api_key and self.api_secret)
        if not api_keys_present:
            issues.append("API ключи не настроены")
        
        # Логируем критически важную информацию о режиме торговли
        log("=" * 60, "CONFIG")
        log("🔴 РЕЖИМ ТОРГОВЛИ: РЕАЛЬНЫЙ", "CONFIG")
        log("⚠️  ВНИМАНИЕ: Будут выполняться РЕАЛЬНЫЕ торговые операции!", "CONFIG")
        log("💰 Убедитесь что API ключи настроены корректно", "CONFIG")
        if not api_keys_present:
            log("❌ КРИТИЧЕСКАЯ ОШИБКА: API ключи не настроены!", "ERROR")
            issues.append("Для работы бота необходимы API ключи")
        log("=" * 60, "CONFIG")
        
        # Создаем статус конфигурации
        self.config_status = ConfigurationStatus(
            api_keys_present=api_keys_present,
            api_keys_valid=False,  # Будет проверено позже
            environment_source="mixed",  # Смешанный источник
            configuration_issues=issues,
            safety_checks_passed=len(issues) == 0
        )
    
    def get_trading_mode(self) -> str:
        """Определение режима торговли с диагностикой"""
        return "LIVE"
    
    def log_configuration_status(self):
        """Подробное логирование текущей конфигурации"""
        if self.config_status:
            log("📊 СТАТУС КОНФИГУРАЦИИ:", "CONFIG")
            log(f"   Режим торговли: РЕАЛЬНЫЙ", "CONFIG")
            log(f"   API ключи присутствуют: {'✅' if self.config_status.api_keys_present else '❌'}", "CONFIG")
            log(f"   Проверки безопасности: {'✅' if self.config_status.safety_checks_passed else '❌'}", "CONFIG")
            if self.config_status.configuration_issues:
                log(f"   Проблемы: {', '.join(self.config_status.configuration_issues)}", "CONFIG")

class TradingModeController:
    """Контроллер режима торговли с проверками безопасности"""
    
    def __init__(self, config: EnvironmentConfig):
        self.config = config
        self.trading_mode_status = None
        self._update_trading_mode_status()
    
    def _update_trading_mode_status(self):
        """Обновление статуса режима торговли"""
        blocking_issues = []
        
        # Проверяем API ключи
        if not self.config.config_status.api_keys_present:
            blocking_issues.append("API ключи не настроены")
        
        # Проверяем общие проблемы конфигурации
        if self.config.config_status.configuration_issues:
            blocking_issues.extend(self.config.config_status.configuration_issues)
        
        self.trading_mode_status = TradingModeStatus(
            current_mode="LIVE",
            mode_source="конфигурация окружения",
            blocking_issues=blocking_issues,
            last_mode_change=datetime.now(timezone.utc)
        )
    
    def is_test_mode(self) -> bool:
        """Проверка текущего режима торговли"""
        return False
    
    def is_live_mode(self) -> bool:
        """Проверка реального режима торговли"""
        return True
    
    def validate_live_mode_requirements(self) -> bool:
        """Проверка требований для реального режима"""
        # Проверяем API ключи
        if not self.config.config_status.api_keys_present:
            log("❌ ПРОВЕРКА РЕЖИМА: API ключи не настроены", "ERROR")
            return False
        
        # Проверяем общие проблемы конфигурации
        if self.config.config_status.configuration_issues:
            log(f"❌ ПРОВЕРКА РЕЖИМА: {', '.join(self.config.config_status.configuration_issues)}", "ERROR")
            return False
        
        log("✅ ПРОВЕРКА РЕЖИМА: Все требования выполнены", "SUCCESS")
        return True
    
    def get_mode_display_name(self) -> str:
        """Получить отображаемое имя режима"""
        return "РЕАЛЬНЫЙ"
    
    def get_mode_emoji(self) -> str:
        """Получить эмодзи для режима"""
        return "🔴"
    
    def log_trading_mode_status(self):
        """Логирование статуса режима торговли"""
        if self.trading_mode_status:
            log("📊 СТАТУС РЕЖИМА ТОРГОВЛИ:", "CONFIG")
            log(f"   Текущий режим: {self.get_mode_emoji()} {self.trading_mode_status.current_mode}", "CONFIG")
            log(f"   Источник: {self.trading_mode_status.mode_source}", "CONFIG")
            if self.trading_mode_status.blocking_issues:
                log(f"   Блокирующие проблемы: {', '.join(self.trading_mode_status.blocking_issues)}", "CONFIG")
    
    def get_trade_operation_prefix(self) -> str:
        """Получить префикс для торговых операций"""
        return "🔴 LIVE"

class SafetyValidator:
    """Валидатор безопасности для проверок перед торговлей"""
    
    def __init__(self, config: EnvironmentConfig):
        self.config = config
    
    def validate_api_keys(self, api_key: str, api_secret: str) -> bool:
        """Проверка валидности API ключей"""
        if not api_key or not api_secret:
            log("❌ ПРОВЕРКА API КЛЮЧЕЙ: Ключи не предоставлены", "SAFETY")
            return False
        
        # Проверка формата ключей
        if len(api_key) < 20 or len(api_secret) < 20:
            log("❌ ПРОВЕРКА API КЛЮЧЕЙ: Ключи слишком короткие", "SAFETY")
            return False
        
        # Проверка на наличие недопустимых символов
        import re
        if not re.match(r'^[A-Za-z0-9]+$', api_key) or not re.match(r'^[A-Za-z0-9]+$', api_secret):
            log("❌ ПРОВЕРКА API КЛЮЧЕЙ: Ключи содержат недопустимые символы", "SAFETY")
            return False
        
        log("✅ ПРОВЕРКА API КЛЮЧЕЙ: Формат ключей корректный", "SAFETY")
        return True
    
    def check_account_permissions(self, client) -> bool:
        """Проверка разрешений аккаунта для торговли"""
        if not client:
            log("❌ ПРОВЕРКА РАЗРЕШЕНИЙ: Клиент не инициализирован", "SAFETY")
            return False
        
        try:
            # Проверяем статус аккаунта
            account_info = client.get_account()
            
            # Проверяем разрешения на торговлю
            can_trade = account_info.get('canTrade', False)
            if not can_trade:
                log("❌ ПРОВЕРКА РАЗРЕШЕНИЙ: Торговля запрещена для аккаунта", "SAFETY")
                return False
            
            # Проверяем статус аккаунта
            account_type = account_info.get('accountType', 'UNKNOWN')
            log(f"✅ ПРОВЕРКА РАЗРЕШЕНИЙ: Тип аккаунта: {account_type}, торговля разрешена", "SAFETY")
            return True
            
        except Exception as e:
            log(f"❌ ПРОВЕРКА РАЗРЕШЕНИЙ: Ошибка при проверке аккаунта: {e}", "SAFETY")
            return False
    
    def validate_minimum_balance(self, usdt_balance: float, base_balance: float, current_price: float) -> bool:
        """Проверка минимального баланса"""
        total_value = usdt_balance + (base_balance * current_price)
        min_required = self.config.min_balance_usdt
        
        if total_value < min_required:
            log(f"❌ ПРОВЕРКА БАЛАНСА: Недостаточный баланс ${total_value:.2f} < ${min_required:.2f}", "SAFETY")
            return False
        
        log(f"✅ ПРОВЕРКА БАЛАНСА: Баланс достаточный ${total_value:.2f} >= ${min_required:.2f}", "SAFETY")
        return True
    
    def validate_trade_amount(self, amount: float, min_amount: float = 10.0) -> bool:
        """Проверка минимальной суммы для торговли"""
        if amount < min_amount:
            log(f"❌ ПРОВЕРКА СУММЫ ТОРГОВЛИ: Сумма слишком мала ${amount:.2f} < ${min_amount:.2f}", "SAFETY")
            return False
        
        log(f"✅ ПРОВЕРКА СУММЫ ТОРГОВЛИ: Сумма достаточная ${amount:.2f} >= ${min_amount:.2f}", "SAFETY")
        return True
    
    def check_api_connection(self, client) -> bool:
        """Проверка подключения к API"""
        if not client:
            log("❌ ПРОВЕРКА ПОДКЛЮЧЕНИЯ: Клиент не инициализирован", "SAFETY")
            return False
        
        try:
            # Проверяем подключение
            client.ping()
            
            # Проверяем время сервера
            server_time = client.get_server_time()
            local_time = int(time.time() * 1000)
            time_diff = abs(server_time["serverTime"] - local_time)
            
            if time_diff > 5000:  # 5 секунд
                log(f"⚠️ ПРОВЕРКА ПОДКЛЮЧЕНИЯ: Большая разница во времени: {time_diff}мс", "SAFETY")
            
            log("✅ ПРОВЕРКА ПОДКЛЮЧЕНИЯ: API доступно", "SAFETY")
            return True
            
        except Exception as e:
            log(f"❌ ПРОВЕРКА ПОДКЛЮЧЕНИЯ: Ошибка подключения к API: {e}", "SAFETY")
            return False
    
    def perform_safety_checks(self, client=None, usdt_balance: float = 0, base_balance: float = 0, current_price: float = 0) -> List[str]:
        """Выполнение всех проверок безопасности"""
        log("🔒 НАЧАЛО ПРОВЕРОК БЕЗОПАСНОСТИ", "SAFETY")
        log("=" * 50, "SAFETY")
        
        issues = []
        
        # 1. Проверка API ключей
        if not self.validate_api_keys(self.config.api_key or "", self.config.api_secret or ""):
            issues.append("Невалидные API ключи")
        
        # 2. Проверка подключения к API (если клиент предоставлен)
        if client and not self.check_api_connection(client):
            issues.append("Нет подключения к API")
        
        # 3. Проверка разрешений аккаунта (если клиент предоставлен)
        if client and not self.check_account_permissions(client):
            issues.append("Недостаточные разрешения аккаунта")
        
        # 4. Проверка минимального баланса (если данные предоставлены)
        if current_price > 0 and not self.validate_minimum_balance(usdt_balance, base_balance, current_price):
            issues.append("Недостаточный баланс для торговли")
        
        # Итоговый результат
        if issues:
            log("❌ ПРОВЕРКИ БЕЗОПАСНОСТИ ПРОВАЛЕНЫ:", "SAFETY")
            for issue in issues:
                log(f"   - {issue}", "SAFETY")
        else:
            log("✅ ВСЕ ПРОВЕРКИ БЕЗОПАСНОСТИ ПРОЙДЕНЫ", "SAFETY")
        
        log("=" * 50, "SAFETY")
        return issues
    
    def can_perform_live_trading(self, client=None, usdt_balance: float = 0, base_balance: float = 0, current_price: float = 0) -> bool:
        """Проверка возможности выполнения реальной торговли"""
        issues = self.perform_safety_checks(client, usdt_balance, base_balance, current_price)
        return len(issues) == 0


# ========== Staking manager (Simple Earn Flexible) ==========
class StakingManager:
    """Управляет простым стейкингом через Simple Earn Flexible (Binance Simple Earn).

    В LIVE режиме использует методы client.get_simple_earn_flexible_product_list,
    client.subscribe_simple_earn_flexible_product и client.redeem_simple_earn_flexible_product.
    """

    def __init__(self, client: Optional[Client], config: EnvironmentConfig):
        self.client = client
        self.config = config

    def list_flexible_products(self):
        if not self.client:
            log("STAKING: list_flexible_products невозможен - нет клиента", "STAKING")
            return []

        try:
            return self.client.get_simple_earn_flexible_product_list()
        except Exception as e:
            log(f"Ошибка получения списка продуктов Simple Earn: {e}", "ERROR")
            return []

    def find_product_for_asset(self, asset: str):
        prods = self.list_flexible_products()
        for p in prods:
            if p.get("asset") == asset and p.get("canPurchase", False):
                return p
        return None

    def stake(self, asset: str, amount: float) -> Dict[str, Any]:
        """Subscribe to flexible product. Returns result dict."""
        if not self.client:
            log(f"STAKING: невозможно стейкать {amount} {asset} - нет клиента", "STAKING")
            return {"success": False, "error": "no_client"}

        prod = self.find_product_for_asset(asset)
        if not prod:
            log(f"STAKING: продукт для {asset} не найден или нельзя купить", "STAKING")
            return {"success": False, "error": "no_product"}

        params = {"productId": prod.get("productId"), "amount": str(amount)}
        try:
            res = self.client.subscribe_simple_earn_flexible_product(**params)
            log(f"STAKING: подписка {asset} {amount} -> {res}", "STAKING")
            
            # Обработка различных форматов ответа API
            if isinstance(res, dict):
                return {"success": True, "result": res}
            elif isinstance(res, str):
                # Если API вернул строку вместо объекта, создаем стандартизированный ответ
                log(f"STAKING: API вернуло строку вместо объекта: {res}", "WARN")
                return {"success": True, "result": {"message": res}, "amount": amount}
            else:
                return {"success": True, "result": res}
        except Exception as e:
            log(f"Ошибка подписки на стейкинг: {e}", "ERROR")
            return {"success": False, "error": str(e)}

    def unstake(self, asset: str, amount: float) -> Dict[str, Any]:
        """Redeem flexible product. If amount=="ALL" or redeemAll=True, pass that param."""
        if not self.client:
            log(f"STAKING: невозможно выкупить {amount} {asset} - нет клиента", "STAKING")
            return {"success": False, "error": "no_client"}

        prod = self.find_product_for_asset(asset)
        if not prod:
            log(f"STAKING: продукт для {asset} не найден для выкупа", "STAKING")
            return {"success": False, "error": "no_product"}

        params = {"productId": prod.get("productId"), "amount": str(amount), "redeemType": "FAST"}
        try:
            res = self.client.redeem_simple_earn_flexible_product(**params)
            log(f"STAKING: выкуп {asset} {amount} -> {res}", "STAKING")
            
            # Обработка различных форматов ответа API
            if isinstance(res, dict):
                return {"success": True, "result": res}
            elif isinstance(res, str):
                # Если API вернул строку вместо объекта, создаем стандартизированный ответ
                log(f"STAKING: API вернуло строку вместо объекта: {res}", "WARN")
                return {"success": True, "result": {"message": res}, "amount": amount}
            else:
                return {"success": True, "result": res}
        except Exception as e:
            log(f"Ошибка выкупа из стейкинга: {e}", "ERROR")
            return {"success": False, "error": str(e)}

    def get_position(self, asset: str) -> Dict[str, Any]:
        """Получить текущую позицию стейкинга для указанного актива.
        
        Использует client.get_simple_earn_flexible_product_position()
        
        Returns:
            Dict с полями: asset, amount, totalAmount, totalInterest, canRedeem
        """
        if not self.client:
            return {"asset": asset, "amount": 0.0, "totalAmount": 0.0}

        try:
            res = self.client.get_simple_earn_flexible_product_position()
            log(f"STAKING: get_position response: {res}", "DEBUG")
            
            # Обрабатываем разные форматы ответа API
            positions = []
            if isinstance(res, list):
                # Старый формат - прямой список позиций
                positions = res
            elif isinstance(res, dict) and "rows" in res and isinstance(res["rows"], list):
                # Новый формат - словарь с полями 'total' и 'rows'
                positions = res["rows"]
            elif isinstance(res, dict) and "data" in res and isinstance(res["data"], dict) and "list" in res["data"]:
                # Еще более новый формат: {"data": {"list": [...]}}
                positions = res["data"]["list"]
            else:
                log(f"STAKING: API вернул неизвестный формат: {type(res)}", "ERROR")
                log(f"STAKING: Содержимое ответа: {res}", "DEBUG")
                return {"asset": asset, "amount": 0, "error": "unknown_response_format"}
            
            # Фильтруем по активу
            asset_positions = [p for p in positions if p.get("asset") == asset]
            
            if not asset_positions:
                log(f"STAKING: позиции для {asset} не найдены", "STAKING")
                return {"asset": asset, "amount": 0, "totalAmount": 0}
                
            position = asset_positions[0]
            
            # Безопасное извлечение суммы из позиции с учетом разных форматов ответа API
            try:
                amount = 0.0
                # Пробуем поля в порядке вероятности их наличия
                for field in ["totalAmount", "amount", "principalAmount"]:
                    if field in position and position[field] is not None:
                        raw_amount = position[field]
                        if isinstance(raw_amount, (int, float)):
                            amount = float(raw_amount)
                            break
                        elif isinstance(raw_amount, str) and raw_amount.strip():
                            amount = float(raw_amount)
                            break
                
                # Добавляем amount в ответ, если его нет
                if "amount" not in position:
                    position["amount"] = amount
                
                log(f"STAKING: найдена позиция для {asset}, сумма: {amount}", "STAKING")
                return position
                
            except (ValueError, TypeError) as e:
                log(f"STAKING: ошибка извлечения суммы из позиции: {e}, position={position}", "ERROR")
                return {"asset": asset, "amount": 0, "error": str(e), "rawPosition": position}
                
        except Exception as e:
            log(f"Ошибка получения позиции стейкинга: {e}", "ERROR")
            return {"asset": asset, "amount": 0, "error": str(e)}

# ========== Простая логика переключения активов ==========
class AssetSwitcher:
    """Простой класс для переключения между активами по MA сигналам"""
    
    def __init__(self, client: Optional[Client], symbol: str, trading_mode_controller: Optional['TradingModeController'] = None):
        self.client = client
        self.symbol = symbol
        self.base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol.split("USDT")[0]
        self.quote_asset = "USDT"
        # Инициализируем время последнего переключения текущим временем.
        # Ранее использовалось 0, что приводило к очень большим значениям
        # (time_since_last = current_time - 0 =~ seconds since epoch) в логах.
        self.last_switch_time = time.time()
        self.min_switch_interval = 10  # минимум 10 секунд между переключениями
        self.trading_mode_controller = trading_mode_controller
    
    def should_hold_base(self, ma_short: float, ma_long: float) -> bool:
        """Определить, должны ли мы держать базовый актив (коин)"""
        return ma_short > ma_long
    
    def get_current_asset_preference(self, usdt_balance: float, base_balance: float, current_price: float) -> str:
        """Определить какой актив мы сейчас держим"""
        usdt_value = usdt_balance
        base_value = base_balance * current_price
        
        # Логируем детали для диагностики
        log(f"🔍 ОПРЕДЕЛЕНИЕ АКТИВА: USDT=${usdt_value:.2f}, {self.base_asset}=${base_value:.2f}", "DEBUG")
        
        # Считаем что держим тот актив, которого больше по стоимости
        # Используем более низкий порог для определения
        if base_value > usdt_value and base_value > 1.0:  # минимум $1
            log(f"🔍 РЕЗУЛЬТАТ: Держим {self.base_asset} (${base_value:.2f} > ${usdt_value:.2f})", "DEBUG")
            return self.base_asset
        else:
            log(f"🔍 РЕЗУЛЬТАТ: Держим {self.quote_asset} (${usdt_value:.2f} >= ${base_value:.2f})", "DEBUG")
            return self.quote_asset
    
    def need_to_switch(self, current_asset: str, should_hold: str) -> bool:
        """Нужно ли переключать актив"""
        current_time = time.time()
        time_since_last = current_time - self.last_switch_time
        
        log(f"🔍 ПРОВЕРКА ПЕРЕКЛЮЧЕНИЯ: current='{current_asset}', should='{should_hold}', time_since_last={time_since_last:.1f}s", "DEBUG")
        
        # Проверяем кулдаун
        if time_since_last < self.min_switch_interval:
            log(f"🔍 КУЛДАУН АКТИВЕН: {time_since_last:.1f}s < {self.min_switch_interval}s", "DEBUG")
            return False
        
        assets_different = current_asset != should_hold
        log(f"🔍 АКТИВЫ РАЗНЫЕ: {assets_different}", "DEBUG")
        
        return assets_different
    
    def execute_switch(self, from_asset: str, to_asset: str, balance: float, current_price: float, step: float) -> bool:
        """Выполнить переключение актива"""
        try:
            if from_asset == self.base_asset and to_asset == self.quote_asset:
                # Продаем коин за USDT
                return self._sell_base_for_usdt(balance, step)
            elif from_asset == self.quote_asset and to_asset == self.base_asset:
                # Покупаем коин за USDT
                return self._buy_base_with_usdt(balance, current_price, step)
            return False
        except Exception as e:
            log(f"Ошибка переключения {from_asset} -> {to_asset}: {e}", "ERROR")
            return False
    
    def _sell_base_for_usdt(self, base_qty: float, step: float) -> bool:
        """Продать весь базовый актив за USDT"""
        
        if not self.client:
            log(f"❌ Нет подключения к Binance API", "ERROR")
            return False
        
        # Округляем количество согласно требованиям биржи
        qty = round_step(base_qty * 0.999, step)  # 99.9% для учета комиссий
        
        log(f"🔢 РАСЧЕТ ПРОДАЖИ: Исходное количество={base_qty:.6f}, После округления={qty} (step={step})", "CALC")
        
        if qty <= 0:
            log(f"❌ Количество для продажи слишком мало: {qty}", "WARN")
            return False
        
        try:
            # Преобразуем количество в строку с подходящей точностью
            precision = 0
            step_str = str(step)
            if '.' in step_str:
                precision = len(step_str.split('.')[-1])
            
            # Используем строковое представление для точного соответствия требованиям Binance
            qty_str = '{:.{}f}'.format(qty, precision)
            log(f"📤 ОТПРАВКА ОРДЕРА НА ПРОДАЖУ: {qty_str} {self.base_asset} (форматировано с точностью {precision})", "ORDER")
            
            order = self.client.order_market_sell(symbol=self.symbol, quantity=qty_str)
            
            # Подробная информация об ордере
            if 'fills' in order and order['fills']:
                total_usdt = sum(float(fill['price']) * float(fill['qty']) for fill in order['fills'])
                avg_price = total_usdt / float(order['executedQty']) if float(order['executedQty']) > 0 else 0
                log(f"✅ ПРОДАЖА ВЫПОЛНЕНА: {order['executedQty']} {self.base_asset} за {total_usdt:.2f} USDT (средняя цена: {avg_price:.4f})", "TRADE")
            else:
                log(f"✅ ПРОДАЖА ВЫПОЛНЕНА: {qty_str} {self.base_asset} -> USDT", "TRADE")
            
            self.last_switch_time = time.time()
            return True
        except BinanceAPIException as e:
            log(f"❌ ОШИБКА ПРОДАЖИ: {e}", "ERROR")
            # Пробуем с меньшей точностью при ошибке о большой точности
            if "слишком большую точность" in str(e) and precision > 0:
                try:
                    # Пробуем с меньшей точностью
                    new_precision = max(0, precision - 1)
                    qty_str = '{:.{}f}'.format(qty, new_precision)
                    log(f"🔄 ПОВТОРНАЯ ПОПЫТКА с меньшей точностью {new_precision}: {qty_str}", "RETRY")
                    
                    order = self.client.order_market_sell(symbol=self.symbol, quantity=qty_str)
                    log(f"✅ ПРОДАЖА ВЫПОЛНЕНА со второй попытки: {qty_str} {self.base_asset} -> USDT", "TRADE")
                    self.last_switch_time = time.time()
                    return True
                except Exception as retry_e:
                    log(f"❌ ОШИБКА при повторной попытке: {retry_e}", "ERROR")
            return False
        except Exception as e:
            log(f"❌ ОШИБКА ПРОДАЖИ: {e}", "ERROR")
            return False
    
    def _buy_base_with_usdt(self, usdt_amount: float, current_price: float, step: float) -> bool:
        """Купить базовый актив за весь USDT"""
        
        if not self.client:
            log(f"❌ Нет подключения к Binance API", "ERROR")
            return False
        
        # Рассчитываем количество с учетом комиссий
        usdt_to_spend = usdt_amount * 0.999  # 99.9% для учета комиссий
        qty = round_step(usdt_to_spend / current_price, step)
        
        # Определяем точность
        precision = 0
        step_str = str(step)
        if '.' in step_str:
            precision = len(step_str.split('.')[-1])
            
        log(f"🔢 РАСЧЕТ ПОКУПКИ: USDT={usdt_amount:.2f}, К трате={usdt_to_spend:.2f}, Цена={current_price:.4f}, Количество={qty} (step={step}, precision={precision})", "CALC")
        
        if qty <= 0 or usdt_to_spend < 10:  # минимум $10
            log(f"❌ Сумма для покупки слишком мала: {usdt_to_spend:.2f} USDT (минимум $10)", "WARN")
            return False
        
        try:
            # Используем строковое представление для точного соответствия требованиям Binance
            qty_str = '{:.{}f}'.format(qty, precision)
            
            log(f"📤 ОТПРАВКА ОРДЕРА НА ПОКУПКУ: {qty_str} {self.base_asset} за {usdt_to_spend:.2f} USDT", "ORDER")
            order = self.client.order_market_buy(symbol=self.symbol, quantity=qty_str)
            
            # Подробная информация об ордере
            if 'fills' in order and order['fills']:
                total_cost = sum(float(fill['price']) * float(fill['qty']) for fill in order['fills'])
                avg_price = total_cost / float(order['executedQty']) if float(order['executedQty']) > 0 else 0
                log(f"✅ ПОКУПКА ВЫПОЛНЕНА: {order['executedQty']} {self.base_asset} за {total_cost:.2f} USDT (средняя цена: {avg_price:.4f})", "TRADE")
            else:
                log(f"✅ ПОКУПКА ВЫПОЛНЕНА: {usdt_to_spend:.2f} USDT -> {qty_str} {self.base_asset}", "TRADE")
            
            self.last_switch_time = time.time()
            return True
        except BinanceAPIException as e:
            log(f"❌ ОШИБКА ПОКУПКИ: {e}", "ERROR")
            # Пробуем с меньшей точностью при ошибке о большой точности
            if "слишком большую точность" in str(e) and precision > 0:
                try:
                    # Пробуем с меньшей точностью
                    new_precision = max(0, precision - 1)
                    qty_str = '{:.{}f}'.format(qty, new_precision)
                    log(f"🔄 ПОВТОРНАЯ ПОПЫТКА с меньшей точностью {new_precision}: {qty_str}", "RETRY")
                    
                    order = self.client.order_market_buy(symbol=self.symbol, quantity=qty_str)
                    log(f"✅ ПОКУПКА ВЫПОЛНЕНА со второй попытки: {qty_str} {self.base_asset}", "TRADE")
                    self.last_switch_time = time.time()
                    return True
                except Exception as retry_e:
                    log(f"❌ ОШИБКА при повторной попытке: {retry_e}", "ERROR")
            return False
        except Exception as e:
            log(f"❌ ОШИБКА ПОКУПКИ: {e}", "ERROR")
            return False

# ========== Инициализация конфигурации ==========
env_config = EnvironmentConfig()

# Логируем статус конфигурации
env_config.log_configuration_status()

# Экспортируем переменные для обратной совместимости
API_KEY = env_config.api_key
API_SECRET = env_config.api_secret
SYMBOL = env_config.symbol
INTERVAL = env_config.interval
MA_SHORT = env_config.ma_short
MA_LONG = env_config.ma_long
CHECK_INTERVAL = env_config.check_interval
STATE_PATH = env_config.state_path
MA_SPREAD_BPS = env_config.ma_spread_bps
MAX_RETRIES = env_config.max_retries
HEALTH_CHECK_INTERVAL = env_config.health_check_interval
MIN_BALANCE_USDT = env_config.min_balance_usdt

app = Flask(__name__)

# Глобальные переменные
client: Optional[Client] = None
asset_switcher: Optional[AssetSwitcher] = None
indicator_strategy: Optional['IndicatorBasedStrategy'] = None
trading_mode_controller: Optional[TradingModeController] = None
safety_validator: Optional[SafetyValidator] = None
staking_manager: Optional[StakingManager] = None
running = False
last_action_ts = 0
last_health_check = 0
error_count = 0

# Константы для стратегии
STRATEGY_TYPE = "MA_RSI_ATR"  # или "SIMPLE_MA" для старой стратегии
USE_MULTI_TIMEFRAME = True  # Использовать ли несколько таймфреймов

bot_status = {
    "status": "idle", 
    "symbol": SYMBOL, 
    "current_asset": "USDT",  # какой актив держим сейчас
    "should_hold": "USDT",    # какой актив должны держать по стратегии
    "last_update": None,
    "balance_usdt": 0.0,
    "balance_base": 0.0,
    "current_price": 0.0,
    "ma_short": 0.0,
    "ma_long": 0.0,
    "strategy_type": STRATEGY_TYPE,
    "market_state": "unknown",  # Состояние рынка по индикаторам (flet, buy, sell, no_trade)
    "indicators": {},  # Будет содержать значения индикаторов
    "error_count": 0,
    "uptime": 0,
    "last_switch": None,
    "switches_count": 0
}

# ========== Персистентное состояние ==========
def load_state():
    global bot_status
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                bot_status.update(data)
                log("Состояние загружено из state.json", "STATE")
        except Exception as e:
            log(f"Не удалось загрузить состояние: {e}", "WARN")

def save_state():
    try:
        bot_status["last_update"] = datetime.now(timezone.utc).isoformat()
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(bot_status, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Не удалось сохранить состояние: {e}", "WARN")

# ========== Binance клиент ==========
def init_client():
    global client, asset_switcher, trading_mode_controller, staking_manager
    
    # Создаем контроллер режима торговли
    trading_mode_controller = TradingModeController(env_config)
    trading_mode_controller.log_trading_mode_status()
    
    if API_KEY and API_SECRET:
        try:
            client = Client(API_KEY, API_SECRET)
            # синхронизация времени
            server_time = client.get_server_time()
            local_time = int(time.time() * 1000)
            offset = server_time["serverTime"] - local_time
            if abs(offset) > 1000:
                client.timestamp_offset = offset
                log(f"Время синхронизировано, offset={offset}мс", "TIME")
            
            client.ping()
            asset_switcher = AssetSwitcher(client, SYMBOL, trading_mode_controller)
            # Инициализируем менеджер стейкинга
            global staking_manager
            staking_manager = StakingManager(client, env_config)
            
            log("Подключение к Binance успешно", "SUCCESS")
            bot_status["status"] = "connected"
            return True
        except Exception as e:
            log(f"Ошибка подключения к Binance: {e}", "ERROR")
            client = None
            asset_switcher = None
            bot_status["status"] = "connection_error"
            return False
    else:
        log("API ключи не заданы — невозможно использовать бота", "ERROR")
        asset_switcher = AssetSwitcher(None, SYMBOL, trading_mode_controller)
        staking_manager = StakingManager(None, env_config)
        bot_status["status"] = "no_api_keys"
        return False

# ========== Информация по символу и округление ==========
def get_symbol_filters(symbol: str):
    if not client:
        return 0.001, 0.01, 0.001, 10.0
    
    try:
        info = client.get_symbol_info(symbol)
        if not info:
            raise RuntimeError(f"Не найден символ {symbol}")
        
        lot = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        pricef = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
        min_notional = next((f for f in info["filters"] if f["filterType"] == "MIN_NOTIONAL"), None)
        
        step = float(lot["stepSize"])
        tick = float(pricef["tickSize"])
        min_qty = float(lot["minQty"])
        min_not = float(min_notional["minNotional"]) if min_notional else 10.0
        
        # Подробное логирование фильтров
        log(f"ФИЛЬТРЫ СИМВОЛА {symbol}:", "INFO")
        log(f"  - Step Size (LOT_SIZE): {step}", "INFO")
        log(f"  - Tick Size (PRICE_FILTER): {tick}", "INFO")
        log(f"  - Min Quantity: {min_qty}", "INFO")
        log(f"  - Min Notional: {min_not}", "INFO")
        
        return step, tick, min_qty, min_not
    except Exception as e:
        log(f"Ошибка получения фильтров символа: {e}", "ERROR")
        return 0.001, 0.01, 0.001, 10.0

def round_step(qty: float, step: float) -> float:
    # Определение точности на основе шага
    precision = 0
    step_str = str(step)
    if '.' in step_str:
        precision = len(step_str.split('.')[-1])
    
    rounded = math.floor(qty / step) * step
    # Форматируем с нужной точностью
    return float('{:.{}f}'.format(rounded, precision))

def round_tick(price: float, tick: float) -> float:
    return round(math.floor(price / tick) * tick, 8)

def retry_on_error(func, max_retries=None, delay=1):
    """Повторяет выполнение функции при ошибках.

    Примечание: не используем глобальную константу `MAX_RETRIES` как значение
    по умолчанию в сигнатуре функции, чтобы избежать NameError при импорте
    модуля (значения по умолчанию вычисляются во время определения функции).
    """
    if max_retries is None:
        # Берём глобальное значение, если оно уже определено, иначе используем 3
        max_retries = globals().get("MAX_RETRIES", 3)

    for attempt in range(max_retries):
        try:
            return func()
        except (BinanceAPIException, BinanceOrderException) as e:
            if "Too many requests" in str(e) or "Request rate limit" in str(e):
                wait_time = delay * (2 ** attempt)
                log(f"Rate limit, ждем {wait_time}с (попытка {attempt + 1}/{max_retries})", "WARN")
                time.sleep(wait_time)
            else:
                log(f"Binance ошибка (попытка {attempt + 1}/{max_retries}): {e}", "ERROR")
                if attempt < max_retries - 1:
                    time.sleep(delay)
        except Exception as e:
            log(f"Неожиданная ошибка (попытка {attempt + 1}/{max_retries}): {e}", "ERROR")
            if attempt < max_retries - 1:
                time.sleep(delay)
    
    raise RuntimeError(f"Не удалось выполнить операцию после {max_retries} попыток")

# ========== Данные и MA ==========
BINANCE_INTERVALS = {
    "1m": Client.KLINE_INTERVAL_1MINUTE,
    "3m": Client.KLINE_INTERVAL_3MINUTE,
    "5m": Client.KLINE_INTERVAL_5MINUTE,
    "15m": Client.KLINE_INTERVAL_15MINUTE,
    "30m": Client.KLINE_INTERVAL_30MINUTE,
    "1h": Client.KLINE_INTERVAL_1HOUR,
    "4h": Client.KLINE_INTERVAL_4HOUR,
}

def get_closes(symbol: str, interval: str, limit: int = 200):
    def _get_klines():
        inter = BINANCE_INTERVALS.get(interval, Client.KLINE_INTERVAL_5MINUTE)
        klines = client.get_klines(symbol=symbol, interval=inter, limit=limit)
        return [float(k[4]) for k in klines]
    
    return retry_on_error(_get_klines)

def get_klines_data(symbol: str, interval: str, limit: int = 200):
    """
    Получить полные данные о свечах (OHLCV)
    """
    
    def _get_full_klines():
        inter = BINANCE_INTERVALS.get(interval, Client.KLINE_INTERVAL_5MINUTE)
        klines = client.get_klines(symbol=symbol, interval=inter, limit=limit)
        
        data = {
            'open': [float(k[1]) for k in klines],
            'high': [float(k[2]) for k in klines],
            'low': [float(k[3]) for k in klines],
            'close': [float(k[4]) for k in klines],
            'volume': [float(k[5]) for k in klines]
        }
        
        return data
    
    return retry_on_error(_get_full_klines)

def ma(arr, period):
    if len(arr) < period:
        return None
    return sum(arr[-period:]) / period

# ========== Балансы ==========
def get_balances() -> Tuple[float, float]:
    def _get_balances():
        base = SYMBOL[:-4] if SYMBOL.endswith("USDT") else SYMBOL.split("USDT")[0]
        usdt = float(client.get_asset_balance("USDT")["free"])
        base_bal = float(client.get_asset_balance(base)["free"])
        return usdt, base_bal
    
    return retry_on_error(_get_balances)

# ========== Проверка здоровья системы ==========
def health_check():
    global last_health_check, error_count
    current_time = time.time()
    
    if current_time - last_health_check > HEALTH_CHECK_INTERVAL:
        try:
            if client:
                client.ping()
                usdt_bal, base_bal = get_balances()
                bot_status.update({
                    "balance_usdt": usdt_bal,
                    "balance_base": base_bal,
                    "error_count": error_count
                })
                
                if error_count > 0:
                    error_count = max(0, error_count - 1)
                    
            last_health_check = current_time
            log("Проверка здоровья системы пройдена", "HEALTH")
        except Exception as e:
            log(f"Ошибка проверки здоровья: {e}", "ERROR")
            error_count += 1

# ========== Основной торговый цикл ==========
def trading_loop():
    global running, last_action_ts, bot_status, error_count, indicator_strategy
    
    start_time = time.time()
    log(f"Старт торгового цикла для {SYMBOL}", "START")
    
    # Убеждаемся что running = True
    if not running:
        log("⚠️ running=False, устанавливаем в True", "WARN")
        running = True
    
    # Получаем фильтры символа
    step, tick, min_qty, min_notional = get_symbol_filters(SYMBOL)
    load_state()
    
    # Инициализируем asset_switcher если не инициализирован
    global asset_switcher
    if asset_switcher is None:
        log("🔧 Инициализация AssetSwitcher...", "INIT")
        asset_switcher = AssetSwitcher(client, SYMBOL)
    
    # Инициализируем стратегию индикаторов, если используется новая стратегия
    if STRATEGY_TYPE == "MA_RSI_ATR" and indicator_strategy is None:
        try:
            log("🔧 Инициализация IndicatorBasedStrategy...", "INIT")
            indicator_strategy = IndicatorBasedStrategy(SYMBOL)
        except Exception as e:
            log(f"⚠️ Ошибка инициализации IndicatorBasedStrategy: {e}. Будет использована базовая стратегия MA.", "ERROR")
            # Откатываемся к простой стратегии MA
            bot_status["strategy_type"] = "SIMPLE_MA"
    
    cycle_count = 0
    log(f"🔄 Начинаем основной цикл торговли (running={running})", "LOOP")
    
    while running:
        try:
            cycle_count += 1
            log(f"🔄 ЦИКЛ #{cycle_count} ==========================================", "CYCLE")
            
            # Обновляем время работы
            bot_status["uptime"] = int(time.time() - start_time)
            
            # Проверка здоровья системы
            health_check()
            
            # Получаем данные
            log("📊 Получение рыночных данных...", "DATA")
            prices = get_closes(SYMBOL, INTERVAL, limit=max(MA_LONG * 3, 100))
            price = prices[-1]
            usdt_bal, base_bal = get_balances()
            
            # Для стратегии MA_RSI_ATR получаем данные по разным таймфреймам
            data_multi_tf = {}
            if bot_status["strategy_type"] == "MA_RSI_ATR" and USE_MULTI_TIMEFRAME:
                log("📊 Получение данных для мульти-таймфреймового анализа...", "DATA")
                try:
                    data_multi_tf = {
                        "30m": get_klines_data(SYMBOL, "30m", 100),
                        "1h": get_klines_data(SYMBOL, "1h", 100),
                        "4h": get_klines_data(SYMBOL, "4h", 100)
                    }
                    log(f"📊 Получено данных по таймфреймам: 30m ({len(data_multi_tf['30m']['close'])}), 1h ({len(data_multi_tf['1h']['close'])}), 4h ({len(data_multi_tf['4h']['close'])})", "DATA")
                except Exception as e:
                    log(f"⚠️ Ошибка получения мульти-таймфреймовых данных: {e}", "ERROR")
            
            # Подробный лог балансов
            base_value = base_bal * price
            total_value = usdt_bal + base_value
            log(f"💰 БАЛАНСЫ: USDT={usdt_bal:.2f} | {asset_switcher.base_asset}={base_bal:.6f} (${base_value:.2f}) | ВСЕГО=${total_value:.2f}", "BALANCE")
            
            # Обновляем статус
            bot_status.update({
                "current_price": price,
                "balance_usdt": usdt_bal,
                "balance_base": base_bal
            })
            
            # Проверяем минимальный баланс
            if total_value < MIN_BALANCE_USDT:
                log(f"❌ Недостаточный общий баланс для торговли: ${total_value:.2f} < ${MIN_BALANCE_USDT}", "WARN")
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Определяем какой актив держим сейчас (одинаково для всех стратегий)
            if asset_switcher is None:
                log("❌ AssetSwitcher не инициализирован", "ERROR")
                time.sleep(CHECK_INTERVAL)
                continue
            
            current_asset = asset_switcher.get_current_asset_preference(usdt_bal, base_bal, price)
            bot_status["current_asset"] = current_asset
            
            # Выбор стратегии и анализ данных
            should_hold_base = False  # По умолчанию держим USDT
            should_hold_asset = asset_switcher.quote_asset
            market_state = "no_trade"
            
            if bot_status["strategy_type"] == "MA_RSI_ATR" and indicator_strategy is not None and USE_MULTI_TIMEFRAME:
                # ==== РАСШИРЕННАЯ СТРАТЕГИЯ MA_RSI_ATR ====
                log("📊 Анализ данных по MA_RSI_ATR стратегии...", "STRATEGY")
                
                try:
                    if all(len(data_multi_tf.get(tf, {}).get('close', [])) > 25 for tf in ['30m', '1h', '4h']):
                        # Получаем анализ от нашей индикаторной системы
                        analysis = indicator_strategy.analyze(data_multi_tf['30m'], data_multi_tf['1h'], data_multi_tf['4h'])
                        
                        # Сохраняем результаты анализа в статус
                        should_hold_base = analysis["should_hold_base"]
                        should_hold_asset = asset_switcher.base_asset if should_hold_base else asset_switcher.quote_asset
                        market_state = analysis["market_state"]
                        
                        # Подробный лог индикаторов
                        log(f"📈 ИНДИКАТОРЫ: 30m сигнал={analysis['30m']['signal']}, 1h RSI={analysis['1h']['rsi']:.1f}, ATR={analysis['1h']['atr_percent']:.2f}%, 4h тренд={analysis['4h']['trend']}", "INDICATORS")
                        log(f"🎯 РЕШЕНИЕ: {market_state} → Должны держать {should_hold_asset}", "STRATEGY")
                        
                        # Сохраняем значения индикаторов в статусе бота
                        bot_status.update({
                            "market_state": market_state,
                            "should_hold": should_hold_asset,
                            "indicators": {
                                "30m": {
                                    "signal": analysis['30m']['signal'],
                                    "ma7": analysis['30m']['ma7'],
                                    "ma25": analysis['30m']['ma25'],
                                },
                                "1h": {
                                    "rsi": analysis['1h']['rsi'],
                                    "atr": analysis['1h']['atr'],
                                    "atr_percent": analysis['1h']['atr_percent'],
                                },
                                "4h": {
                                    "trend": analysis['4h']['trend'],
                                    "ma7": analysis['4h']['ma7'],
                                    "ma25": analysis['4h']['ma25'],
                                },
                            }
                        })
                        
                        # Если рынок в состоянии флета, не торгуем
                        if market_state == "flet":
                            log(f"🔇 ФЛЕТ: Рынок в боковике, торговля приостановлена", "FILTER")
                            time.sleep(CHECK_INTERVAL)
                            continue
                        
                        # Если нет четкого сигнала, продолжаем держать текущую позицию
                        if market_state == "no_trade":
                            log(f"🔇 НЕТ СИГНАЛА: Продолжаем держать {current_asset}", "FILTER")
                            time.sleep(CHECK_INTERVAL)
                            continue
                    else:
                        log("⚠️ Недостаточно данных для мульти-таймфреймового анализа, используем классическую MA стратегию", "WARN")
                        # Если недостаточно данных, используем простую MA стратегию
                        bot_status["strategy_type"] = "SIMPLE_MA"
                except Exception as e:
                    log(f"❌ Ошибка в MA_RSI_ATR стратегии: {e}, возвращаемся к базовой стратегии", "ERROR")
                    bot_status["strategy_type"] = "SIMPLE_MA"
            
            # Если мы здесь, то либо используем SIMPLE_MA, либо возникла ошибка в MA_RSI_ATR
            if bot_status["strategy_type"] != "MA_RSI_ATR" or market_state == "no_trade":
                # ==== КЛАССИЧЕСКАЯ MA СТРАТЕГИЯ ====
                # Рассчитываем MA
                m1 = ma(prices, MA_SHORT)
                m2 = ma(prices, MA_LONG)
                
                if m1 is not None and m2 is not None:
                    # Подробный лог MA
                    ma_diff = m1 - m2
                    ma_diff_pct = (ma_diff / price) * 100
                    spread_bps = abs(ma_diff / price) * 10000.0
                    
                    log(f"📈 MA АНАЛИЗ: MA7={m1:.4f} | MA25={m2:.4f} | Разница={ma_diff:+.4f} ({ma_diff_pct:+.3f}%) | Спред={spread_bps:.1f}б.п.", "MA")
                    
                    bot_status.update({
                        "ma_short": m1,
                        "ma_long": m2
                    })
                    
                    # Определяем какой актив должны держать
                    should_hold_base = asset_switcher.should_hold_base(m1, m2)
                    should_hold_asset = asset_switcher.base_asset if should_hold_base else asset_switcher.quote_asset
                    
                    # Подробный лог стратегии
                    trend_direction = "ВОСХОДЯЩИЙ 📈" if m1 > m2 else "НИСХОДЯЩИЙ 📉"
                    strategy_reason = f"MA7 {'>' if m1 > m2 else '<'} MA25"
                    log(f"🎯 СТРАТЕГИЯ: {trend_direction} ({strategy_reason}) → Должны держать {should_hold_asset}", "STRATEGY")
                    
                    # Обновляем статус
                    bot_status.update({
                        "should_hold": should_hold_asset,
                        "market_state": "buy" if should_hold_base else "sell"
                    })
                    
                    # Проверяем фильтр шума
                    if spread_bps < MA_SPREAD_BPS:
                        log(f"🔇 ФИЛЬТР ШУМА: Спред {spread_bps:.1f}б.п. < {MA_SPREAD_BPS}б.п. - сигнал слишком слабый", "FILTER")
                        time.sleep(CHECK_INTERVAL)
                        continue
                else:
                    log("❌ Недостаточно данных для расчета MA", "ERROR")
                    time.sleep(CHECK_INTERVAL)
                    continue
            
            log(f"🏦 ТЕКУЩИЙ АКТИВ: {current_asset} (по балансам: USDT=${usdt_bal:.2f}, {asset_switcher.base_asset}=${base_value:.2f})", "CURRENT")
            
            # Проверяем кулдаун
            time_since_last_switch = time.time() - asset_switcher.last_switch_time
            if time_since_last_switch < asset_switcher.min_switch_interval:
                remaining_cooldown = asset_switcher.min_switch_interval - time_since_last_switch
                log(f"⏰ КУЛДАУН: Осталось {remaining_cooldown:.1f}сек до следующего переключения", "COOLDOWN")
                time.sleep(CHECK_INTERVAL)
                continue
            
            # Итоговый статус
            status_emoji = "✅ СИНХРОНИЗИРОВАНО" if current_asset == should_hold_asset else "⚠️ ТРЕБУЕТСЯ ПЕРЕКЛЮЧЕНИЕ"
            log(f"📊 СТАТУС: Цена={price:.4f} | Держим={current_asset} | Нужно={should_hold_asset} | {status_emoji}", "STATUS")
            
            # Подробная диагностика переключения
            log(f"🔍 ДИАГНОСТИКА: current_asset='{current_asset}', should_hold_asset='{should_hold_asset}'", "DEBUG")
            log(f"🔍 БАЛАНСЫ: USDT={usdt_bal:.2f}, {asset_switcher.base_asset}={base_bal:.6f} (${base_value:.2f})", "DEBUG")
            log(f"🔍 КУЛДАУН: Прошло {time_since_last_switch:.1f}сек с последнего переключения (мин: {asset_switcher.min_switch_interval}сек)", "DEBUG")
            
            # Проверяем нужно ли переключать актив
            need_switch = asset_switcher.need_to_switch(current_asset, should_hold_asset)
            log(f"🔍 РЕШЕНИЕ: need_to_switch = {need_switch}", "DEBUG")
            
            if need_switch:
                log(f"🔄 ПЕРЕКЛЮЧЕНИЕ ТРЕБУЕТСЯ: {current_asset} → {should_hold_asset}", "SWITCH")
                
                # Подробная информация о переключении
                if current_asset == asset_switcher.base_asset:
                    # Продаем базовый актив
                    log(f"📉 ПРОДАЖА: {base_bal:.6f} {asset_switcher.base_asset} → USDT по цене {price:.4f}", "TRADE_PLAN")
                    expected_usdt = base_bal * price * 0.999  # с учетом комиссии
                    log(f"💵 ОЖИДАЕМЫЙ РЕЗУЛЬТАТ: ~{expected_usdt:.2f} USDT (с учетом комиссии 0.1%)", "TRADE_PLAN")
                    
                    success = asset_switcher.execute_switch(
                        current_asset, should_hold_asset, base_bal, price, step
                    )
                else:
                    # Покупаем базовый актив
                    log(f"📈 ПОКУПКА: {usdt_bal:.2f} USDT → {asset_switcher.base_asset} по цене {price:.4f}", "TRADE_PLAN")
                    expected_qty = (usdt_bal * 0.999) / price  # с учетом комиссии
                    log(f"🪙 ОЖИДАЕМЫЙ РЕЗУЛЬТАТ: ~{expected_qty:.6f} {asset_switcher.base_asset} (с учетом комиссии 0.1%)", "TRADE_PLAN")
                    
                    success = asset_switcher.execute_switch(
                        current_asset, should_hold_asset, usdt_bal, price, step
                    )
                
                if success:
                    bot_status["switches_count"] = bot_status.get("switches_count", 0) + 1
                    bot_status["last_switch"] = datetime.now(timezone.utc).isoformat()
                    last_action_ts = time.time()
                    log(f"✅ ПЕРЕКЛЮЧЕНИЕ ВЫПОЛНЕНО УСПЕШНО! Общее количество переключений: {bot_status['switches_count']}", "SUCCESS")
                    
                    # Ждем немного для обновления балансов на бирже
                    time.sleep(2)
                    
                    # Логируем новые балансы после переключения
                    new_usdt_bal, new_base_bal = get_balances()
                    new_base_value = new_base_bal * price
                    new_total = new_usdt_bal + new_base_value
                    log(f"💰 НОВЫЕ БАЛАНСЫ: USDT={new_usdt_bal:.2f} | {asset_switcher.base_asset}={new_base_bal:.6f} (${new_base_value:.2f}) | ВСЕГО=${new_total:.2f}", "RESULT")
                    
                    # Обновляем статус с новыми балансами
                    bot_status.update({
                        "balance_usdt": new_usdt_bal,
                        "balance_base": new_base_bal
                    })
                else:
                    log(f"❌ ОШИБКА ПЕРЕКЛЮЧЕНИЯ! Будет повторная попытка в следующем цикле.", "ERROR")
                    error_count += 1
                    # Добавляем информацию в bot_status для диагностики
                    bot_status["last_error"] = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "action": f"switch_{current_asset}_to_{should_hold_asset}",
                            "error_count": error_count
                        }
                # ========== Staking: если включено, проверяем возможности ==========
                try:
                    if env_config.enable_staking and staking_manager:
                        log(f"STAKING: Проверка возможностей стейкинга (enable_staking={env_config.enable_staking}, stake_all={env_config.stake_all})", "STAKING")
                        # Если стратегия требует держать базовый актив — стейкаем часть свободного базового
                        if should_hold_asset == asset_switcher.base_asset:
                            # Сначала получаем текущую позицию в стейкинге
                            current_staking_position = staking_manager.get_position(asset_switcher.base_asset)
                            current_staked_amount = 0.0
                            
                            # Безопасно извлекаем текущую застейканную сумму
                            try:
                                # Извлекаем сумму из позиции с обработкой разных форматов полей
                                for field in ["totalAmount", "amount", "principalAmount"]:
                                    if field in current_staking_position and current_staking_position[field] is not None:
                                        raw_amount = current_staking_position[field]
                                        if isinstance(raw_amount, (int, float)):
                                            current_staked_amount = float(raw_amount)
                                            break
                                        elif isinstance(raw_amount, str) and raw_amount.strip():
                                            current_staked_amount = float(raw_amount)
                                            break
                            except (ValueError, TypeError) as e:
                                log(f"STAKING: ошибка извлечения текущей позиции стейкинга: {e}", "ERROR")
                            
                            log(f"STAKING: текущая позиция {asset_switcher.base_asset} в стейкинге: {current_staked_amount}", "STAKING")
                            
                            # если включено STAKE_ALL — стейкаем весь базовый баланс
                            if env_config.stake_all:
                                stake_amount = base_bal
                            else:
                                # available base balance (оставляем резерв для торговли)
                                reserve_usdt = env_config.min_balance_usdt
                                # конвертируем reserve в базовый asset
                                reserve_base = reserve_usdt / price if price > 0 else 0
                                free_base = max(0.0, base_bal - reserve_base)
                                stake_amount = free_base * env_config.stake_percent
                                
                            # Проверяем, есть ли что стейкать с учетом текущей позиции
                            if current_staked_amount > 0:
                                log(f"STAKING: у вас уже застейкано {current_staked_amount} {asset_switcher.base_asset}", "STAKING")
                                # Обновляем состояние в bot_status для отслеживания
                                bot_status.setdefault("staking", {})
                                bot_status["staking"][asset_switcher.base_asset] = current_staked_amount
                                save_state()
                            elif stake_amount > 0:
                                stake_value_usdt = stake_amount * price
                                if stake_value_usdt >= env_config.min_stake_usdt:
                                    log(f"STAKING: пытаемся стейкать {stake_amount:.6f} {asset_switcher.base_asset} (~${stake_value_usdt:.2f})", "STAKING")
                                    res = staking_manager.stake(asset_switcher.base_asset, stake_amount)
                                    bot_status.setdefault("staking", {})
                                    # Обрабатываем результат операции
                                    if res.get("success"):
                                        # В LIVE режиме просто добавляем stake_amount
                                        staked_amount = stake_amount
                                        bot_status["staking"][asset_switcher.base_asset] = bot_status["staking"].get(asset_switcher.base_asset, 0.0) + staked_amount
                                        log(f"STAKING: добавлено {staked_amount} {asset_switcher.base_asset} в стейкинг", "STAKING")
                                    save_state()
                        else:
                            # если нужно держать USDT и у нас есть стейкнутые базовые — выкупаем
                            st_pos = staking_manager.get_position(asset_switcher.base_asset)
                            st_amount = 0
                            # Безопасно извлекаем сумму из позиции с обработкой разных форматов полей
                            try:
                                for field in ["totalAmount", "amount", "principalAmount"]:
                                    if field in st_pos and st_pos[field] is not None:
                                        raw_amount = st_pos[field]
                                        if isinstance(raw_amount, (int, float)):
                                            st_amount = float(raw_amount)
                                            break
                                        elif isinstance(raw_amount, str) and raw_amount.strip():
                                            st_amount = float(raw_amount)
                                            break
                            except (ValueError, TypeError):
                                log(f"STAKING: ошибка преобразования полей st_pos в число: {st_pos}", "ERROR")
                                st_amount = 0
                                
                            if st_amount > 0:
                                log(f"STAKING: инициируем выкуп {st_amount} {asset_switcher.base_asset}", "STAKING")
                                res = staking_manager.unstake(asset_switcher.base_asset, st_amount)
                                # флаг ожидания можно записать в bot_status
                                bot_status.setdefault("staking", {})
                                if res.get("success") and res.get("simulated"):
                                    bot_status["staking"][asset_switcher.base_asset] = 0.0
                                    log(f"STAKING: симуляция выкупа {st_amount} {asset_switcher.base_asset}", "STAKING")
                                elif res.get("success"):
                                    log(f"STAKING: инициирован выкуп {st_amount} {asset_switcher.base_asset}", "STAKING")
                                save_state()
                except Exception as e:
                    log(f"Ошибка в логике стейкинга: {e}", "ERROR")
                    
            # Если не нужно было переключать активы
            if not need_switch:
                log(f"✅ ПЕРЕКЛЮЧЕНИЕ НЕ ТРЕБУЕТСЯ - активы синхронизированы", "OK")
            
            # Обновляем статус
            bot_status["status"] = "running"
            save_state()
            
            log(f"😴 ОЖИДАНИЕ {CHECK_INTERVAL} секунд до следующего цикла...", "SLEEP")
            time.sleep(CHECK_INTERVAL)
            
        except (BinanceAPIException, BinanceOrderException) as e:
            emsg = str(e)
            if "Too many requests" in emsg or "Request rate limit" in emsg:
                log(f"Rate limit: {e} — сплю 5 сек", "WARN")
                time.sleep(5)
            else:
                log(f"Binance ошибка: {e}", "ERROR")
                error_count += 1
                time.sleep(2)
        except Exception as e:
            log(f"Неожиданная ошибка: {e}", "ERROR")
            error_count += 1
            bot_status["status"] = f"error: {str(e)}"
            save_state()
            time.sleep(2)
    
    log("Торговый бот остановлен", "SHUTDOWN")

# ========== Flask маршруты ==========
@app.route("/")
def root():
    return jsonify({
        "ok": True, 
        "symbol": SYMBOL, 
        "status": bot_status.get("status", "idle"), 
        "current_asset": bot_status.get("current_asset", "USDT"),
        "should_hold": bot_status.get("should_hold", "USDT"),
        "mode": "LIVE",
        "uptime": bot_status.get("uptime", 0)
    })

@app.route("/health")
def health():
    try:
        if client:
            client.ping()
            return jsonify({"ok": True, "status": "healthy"})
        else:
            return jsonify({"ok": False, "status": "no_client"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/start")
def start():
    global running, bot_status
    if running:
        return jsonify({"ok": True, "message": "уже работает"})
    
    if API_KEY and API_SECRET:
        init_client()
    else:
        return jsonify({"ok": False, "message": "API ключи не настроены"})
    
    running = True
    bot_status["status"] = "running"
    save_state()
    
    t = threading.Thread(target=trading_loop, daemon=True)
    t.start()
    log("Бот запущен", "START")
    return jsonify({"ok": True, "mode": "LIVE"})

@app.route("/stop")
def stop():
    global running, bot_status
    running = False
    bot_status["status"] = "stopped"
    save_state()
    log("Бот остановлен", "STOP")
    return jsonify({"ok": True})

@app.route("/status")
def status():
    return jsonify({
        "ok": True,
        "symbol": SYMBOL,
        "mode": "LIVE",
        "status": bot_status.get("status", "idle"),
        "current_asset": bot_status.get("current_asset", "USDT"),
        "should_hold": bot_status.get("should_hold", "USDT"),
        "current_price": bot_status.get("current_price", 0.0),
        "balance_usdt": bot_status.get("balance_usdt", 0.0),
        "balance_base": bot_status.get("balance_base", 0.0),
        "ma_short": bot_status.get("ma_short", 0.0),
        "ma_long": bot_status.get("ma_long", 0.0),
        "error_count": bot_status.get("error_count", 0),
        "uptime": bot_status.get("uptime", 0),
        "switches_count": bot_status.get("switches_count", 0),
        "last_switch": bot_status.get("last_switch"),
        "last_update": bot_status.get("last_update")
    })

@app.route("/config")
def config():
    return jsonify({
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "ma_short": MA_SHORT,
        "ma_long": MA_LONG,
        "check_interval": CHECK_INTERVAL,
        "ma_spread_bps": MA_SPREAD_BPS,
        "min_balance_usdt": MIN_BALANCE_USDT
    })

@app.route("/config-status")
def config_status():
    """Подробная диагностика конфигурации и переменных окружения"""
    
    # Проверяем источники переменных окружения
    env_sources = {}
    critical_vars = ["BINANCE_API_KEY", "BINANCE_API_SECRET", "SYMBOL"]
    
    for var in critical_vars:
        system_value = os.environ.get(var)
        dotenv_value = os.getenv(var)
        
        if system_value is not None:
            source = "системные переменные окружения"
            value = system_value
        elif dotenv_value is not None:
            source = ".env файл"
            value = dotenv_value
        else:
            source = "значение по умолчанию"
            value = "не установлено"
        
        # Маскируем API ключи для безопасности
        if "API" in var and value != "не установлено":
            display_value = f"{value[:8]}...{value[-4:]}" if len(value) > 12 else "установлен"
        else:
            display_value = value
            
        env_sources[var] = {
            "value": display_value,
            "source": source,
            "is_set": value != "не установлено"
        }


        @app.route("/staking-status")
        def staking_status():
            try:
                asset = SYMBOL[:-4] if SYMBOL.endswith("USDT") else SYMBOL.split("USDT")[0]
                if staking_manager:
                    pos = staking_manager.get_position(asset)
                    return jsonify({"ok": True, "position": pos})
                else:
                    return jsonify({"ok": True, "position": bot_status.get("staking", {})})
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
    
    # Статус конфигурации
    config_issues = []
    if not env_config.config_status.api_keys_present:
        config_issues.append("API ключи не настроены")
    
    return jsonify({
        "ok": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trading_mode": {
            "current": "LIVE",
            "source": "конфигурация окружения",
            "warning": "ВНИМАНИЕ: Реальная торговля активна!"
        },
        "environment_variables": env_sources,
        "configuration_status": {
            "api_keys_present": env_config.config_status.api_keys_present,
            "safety_checks_passed": env_config.config_status.safety_checks_passed,
            "issues": config_issues
        },
        "file_status": {
            "env_file_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'),
            "env_file_exists": os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
        },
        "recommendations": [
            "Бот работает в реальном режиме - мониторьте операции",
            "Проверяйте баланс и результаты торговли",
            "Убедитесь что стратегия работает корректно",
            "Проверьте права API ключей в Binance (только спот-торговля)"
        ]
    })

# ========== Автозапуск для деплоя ==========
if API_KEY and API_SECRET:
    try:
        if not running:
            init_client()
            running = True
            bot_thread = threading.Thread(target=trading_loop, daemon=True)
            bot_thread.start()
            log("🚀 Торговый бот запущен автоматически в режиме LIVE", "STARTUP")
    except Exception as e:
        log(f"❌ Ошибка автозапуска бота: {e}", "ERROR")
        running = False
else:
    log("⚠️ Автозапуск бота пропущен: нет API ключей", "WARNING")

# ========== Точка входа ==========
if __name__ == "__main__":
    if API_KEY and API_SECRET:
        init_client()
        
        # Запускаем торговый бот в отдельном потоке
        if not running:
            running = True
            bot_thread = threading.Thread(target=trading_loop, daemon=True)
            bot_thread.start()
            mode = "TEST" if TEST_MODE else "LIVE"
            log(f"🚀 Торговый бот запущен в режиме {mode}", "STARTUP")
    
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
    