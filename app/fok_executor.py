# app/fok_executor.py
# Реализация жесткого FOK (Fill-Or-Kill) ордера с ретраями и контролем дрейфа цены.
# Особенности:
#  - Определяет цену, по которой есть полная ликвидность в стакане для целого объема
#  - Добавляет стартовый запас (slippage_bps) и при отказах постепенно "подтягивает" цену в рамках per_attempt_drift_bps
#  - Контролирует суммарный дрейф (max_total_drift_bps) – при превышении прекращает попытки
#  - Корректно приводит цену/количество к биржевым фильтрам (tickSize / stepSize / minQty / maxQty / minNotional)
#  - Все попытки логируются с причиной отказа (код/сообщение Binance)
#  - Успех только если ордер выполнен ПОЛНОСТЬЮ сразу (FOK). Частичного исполнения не бывает.

from __future__ import annotations
from typing import Dict, Any, Tuple, Optional
import math
import time
import logging

from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_LIMIT,
    TIME_IN_FORCE_FOK,
)
from binance.exceptions import BinanceAPIException, BinanceOrderException

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

# ========================== Утилиты фильтров ==========================

def _get_symbol_filters(client: Client, symbol: str) -> Dict[str, Any]:
    info = client.get_symbol_info(symbol)
    if not info:
        raise ValueError(f"Symbol info not found for {symbol}")
    filters = {f["filterType"]: f for f in info.get("filters", [])}
    return {
        "tick_size": float(filters.get("PRICE_FILTER", {}).get("tickSize", "0.00000001")),
        "step_size": float(filters.get("LOT_SIZE", {}).get("stepSize", "0.00000001")),
        "min_qty": float(filters.get("LOT_SIZE", {}).get("minQty", "0")),
        "max_qty": float(filters.get("LOT_SIZE", {}).get("maxQty", "1000000000")),
        "min_notional": float(filters.get("MIN_NOTIONAL", {}).get("minNotional", "0")),
    }

def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step

def _round_price_for_side(price: float, tick: float, side: str) -> float:
    if tick <= 0:
        return price
    # BUY – округляем вверх, SELL – вниз (повышаем шанс полного фила)
    if side == SIDE_BUY:
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick

def _apply_filters(client: Client, symbol: str, side: str, price: float, qty: float) -> Tuple[float, float]:
    f = _get_symbol_filters(client, symbol)
    price = _round_price_for_side(price, f["tick_size"], side)
    qty = max(_round_step(qty, f["step_size"]), f["min_qty"])
    qty = min(qty, f["max_qty"])
    return price, qty

# ========================== Стакан и ликвидность ==========================

def _price_to_fill_full_qty(orderbook: Dict[str, Any], side: str, quantity: float) -> float:
    """Находит минимальную цену, на которой суммарная ликвидность покрывает весь объём quantity."""
    if side == SIDE_BUY:
        levels = orderbook.get("asks", [])
        accum = 0.0
        for p, q in levels:
            accum += float(q)
            if accum >= quantity:
                return float(p)
        raise ValueError("Недостаточно ликвидности (asks) для полного BUY FOK")
    else:
        levels = orderbook.get("bids", [])
        accum = 0.0
        for p, q in levels:
            accum += float(q)
            if accum >= quantity:
                return float(p)
        raise ValueError("Недостаточно ликвидности (bids) для полного SELL FOK")

# ========================== Min Notional ==========================

def _ensure_min_notional(client: Client, symbol: str, price: float, qty: float) -> float:
    f = _get_symbol_filters(client, symbol)
    notional = price * qty
    if notional + 1e-12 >= f["min_notional"]:
        return qty
    needed_qty = f["min_notional"] / max(price, 1e-12)
    needed_qty = max(_round_step(needed_qty, f["step_size"]), f["min_qty"])
    needed_qty = min(needed_qty, f["max_qty"])
    return needed_qty

# ========================== Основная функция ==========================

def place_limit_order_fok_with_retries(
    client: Client,
    symbol: str,
    side: str,
    quantity: float,
    depth_limit: int = 50,
    slippage_bps: float = 5.0,
    max_retries: int = 3,
    retry_sleep: float = 1.5,
    per_attempt_drift_bps: float = 2.0,
    max_total_drift_bps: float = 20.0,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """Отправляет строгий LIMIT FOK ордер с ретраями и контролем дрейфа."""
    L = logger or log
    last_err = None
    total_drift_bps = 0.0
    base_price_ref = None

    for attempt in range(1, max_retries + 1):
        # 1) Получаем стакан
        book = client.get_order_book(symbol=symbol, limit=depth_limit)
        base_price = _price_to_fill_full_qty(book, side, quantity)
        if base_price_ref is None:
            base_price_ref = base_price

        # 2) Стартовая цена с запасом
        if side == SIDE_BUY:
            raw_price = base_price * (1.0 + slippage_bps / 1e4)
        else:
            raw_price = base_price * (1.0 - slippage_bps / 1e4)

        # 3) Фильтры
        price, qty = _apply_filters(client, symbol, side, raw_price, quantity)

        # 4) MinNotional
        qty = _ensure_min_notional(client, symbol, price, qty)

        L.info(
            "FOK attempt %s/%s: symbol=%s side=%s price=%s qty=%s (base=%s drift_total=%.2f bps)",
            attempt, max_retries, symbol, side,
            f"{price:.12f}", f"{qty:.12f}", f"{base_price_ref:.12f}", total_drift_bps
        )

        try:
            order = client.create_order(
                symbol=symbol,
                side=side,
                type=ORDER_TYPE_LIMIT,
                timeInForce=TIME_IN_FORCE_FOK,
                quantity=f"{qty:.8f}".rstrip("0").rstrip("."),
                price=f"{price:.8f}".rstrip("0").rstrip("."),
            )
            L.info("FOK filled successfully: orderId=%s", order.get("orderId"))
            return order
        except (BinanceAPIException, BinanceOrderException) as e:
            last_err = e
            code = getattr(e, "code", None)
            msg = getattr(e, "message", str(e))
            L.warning("FOK rejected: code=%s message=%s", code, msg)

            # Контроль суммарного дрейфа
            next_bump = per_attempt_drift_bps
            if total_drift_bps + next_bump > max_total_drift_bps:
                L.error(
                    "Abort retries: total drift (%.2f bps) would exceed max_total_drift_bps (%.2f bps).",
                    total_drift_bps + next_bump, max_total_drift_bps
                )
                break
            total_drift_bps += next_bump
            # Незначительная пауза
            time.sleep(retry_sleep)

    if last_err:
        raise last_err
    raise RuntimeError("FOK не выполнен после всех попыток (без кода ошибки)")
