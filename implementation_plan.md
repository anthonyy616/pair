# Pair Strategy Engine - Complete System Design & Implementation Plan

---

## 1. SYSTEM OVERVIEW

### What This System Does

This is a MetaTrader 5 (MT5) automated trading bot for WelTrade synthetic indices. It runs on a FastAPI backend with a vanilla HTML/JS frontend, executing a paired-position strategy with automatic PnL-based liquidation monitoring.

### The Strategy In Plain English

1. When the user clicks Start, the bot opens a **Buy** and a **Sell** simultaneously at the current market price
2. It then waits for price to move by a configured "grid distance" in either direction
3. Once price moves that distance, it opens another **Sell** and **Buy** simultaneously — completing two "pairs"
4. It monitors all 4 positions for TP/SL hits. On the **first** TP hit, it opens a single recovery Buy position
5. Throughout, it calculates the exact price levels where total profit or total loss (in $) would hit configured thresholds
6. When max profit or max loss is reached → close everything, restart immediately at current price
7. There is one **exception**: if both pairs are complete and the first TP hit happens at a price >= the start price, skip the single Buy and go straight to nuclear reset + restart

### Architecture Stack

- **Frontend**: Vanilla HTML/CSS/JS + Tailwind CSS (`static/index.html`)
- **Backend API**: FastAPI (`api/server.py`)
- **Config**: JSON file per user (`core/config_manager.py`)
- **Orchestrator**: Routes ticks to per-symbol engines (`core/strategy_orchestrator.py`)
- **Tick Loop**: Polls MT5 for ticks, broadcasts to orchestrators (`core/trading_engine.py`)
- **Strategy Engine**: The core trading logic — THIS IS WHAT WE'RE BUILDING (`core/engine/pair_strategy_engine.py`)
- **Persistence**: SQLite via repository pattern (`core/persistence/repository.py`)
- **MT5 Integration**: Direct via `MetaTrader5` Python package

---

## 2. THE STRATEGY — DETAILED WALKTHROUGH

### 2.1 Naming Convention

- **Pair X** = a Buy position (Bx) and a Sell position (Sx)
- **Pair Y** = a Sell position (Sy) and a Buy position (By)
- **Single Buy** = a standalone recovery position opened on first TP hit

### 2.2 Complete Flow With Numbers

**Config**: grid_distance=50, tp_pips=150, sl_pips=200, bx_lot=0.10, sy_lot=0.10, sx_lot=0.05, by_lot=0.05, single_buy_lot=0.03

**Phase 1 — First Atomic Fire (Start Click)**

Current ask = 1000.00

| Position | Direction | Entry | Lot | TP | SL |
|----------|-----------|-------|-----|----|----|
| Bx | BUY | 1000 | 0.10 | 1150 | 800 |
| Sy | SELL | 1000 | 0.10 | 850 | 1200 |

- `start_price = 1000` (recorded for exception check later)
- Bot transitions to AWAITING_SECOND phase

**Phase 2 — Second Atomic Fire (Grid Distance Reached)**

Price rises to 1050 (= start_price + grid_distance):

| Position | Direction | Entry | Lot | TP | SL |
|----------|-----------|-------|-----|----|----|
| Sx | SELL | 1050 | 0.05 | 900 | 1250 |
| By | BUY | 1050 | 0.05 | 1200 | 850 |

Now both pairs are complete:

- **Pair X**: Bx(BUY@1000, 0.10) + Sx(SELL@1050, 0.05)
- **Pair Y**: Sy(SELL@1000, 0.10) + By(BUY@1050, 0.05)

Liquidation prices are calculated at this point. Bot transitions to PAIRS_COMPLETE phase.

**Phase 3 — First TP Hit**

Say Bx hits TP at price 1150:

- **Exception check**: Are both pairs complete? YES. Is hit_price (1150) >= start_price (1000)? YES.
- **Result**: Nuclear reset → close all positions → auto-restart at current price

Alternative scenario — Say Sy hits TP at price 850:

- **Exception check**: Are both pairs complete? YES. Is hit_price (850) >= start_price (1000)? NO.
- **Result**: Open single Buy at current price with single_buy_lot=0.03, TP/SL from config
- Bot transitions to MONITORING phase, recalculates liquidation prices

**Phase 4 — Ongoing Monitoring**

- Every tick: compare current price vs max_profit_price and max_loss_price
- On any position close: calculate realized PnL, recalculate liquidation prices
- When threshold hit → nuclear reset → auto-restart at current price

### 2.3 The Exception Rule — Detailed

The exception prevents opening a recovery buy when it's not needed:

```
IF both pairs are complete (all 4 positions were opened)
   AND the price at the TP/SL hit >= start_price
THEN → Nuclear Reset + Auto-Restart (do NOT open single Buy)
```

Rationale: If price is back at or above the start level with both pairs complete, the grid cycle has played out. Starting fresh is cleaner than adding a recovery position.

### 2.4 What "Atomic Fire" Means

"Atomic" means both orders are sent back-to-back within the same execution lock. If the first order succeeds but the second fails, the first is NOT rolled back — we retry the second. Both orders are market orders (immediate fill at current ask/bid).

---

## 3. PnL & LIQUIDATION PRICE SYSTEM

### 3.1 The Core Formula

For Deriv synthetic indices, the PnL relationship is:

```
0.01 lot × 100 pips of price movement = $1.00
```

Therefore:

```
PnL per position = (current_price - entry_price) × lot_size   [for BUYS]
PnL per position = (entry_price - current_price) × lot_size   [for SELLS]
```

### 3.2 Total PnL as a Function of Price

```
Total PnL(P) = floating_pnl(P) + realized_pnl

floating_pnl(P) = Σ((P - buy_entry) × buy_lot) + Σ((sell_entry - P) × sell_lot)
                = P × (Σbuy_lots - Σsell_lots) + (Σ(sell_entry × sell_lot) - Σ(buy_entry × buy_lot))
                = P × net_lots + constant

Where:
  net_lots = Σ(buy_lots) - Σ(sell_lots)
  constant = Σ(sell_entry × sell_lot) - Σ(buy_entry × buy_lot)
```

This is a **linear function of price**. So we can solve for the exact price where PnL hits a threshold.

### 3.3 Solving for Liquidation Prices

```
For max_profit:
  P × net_lots + constant + realized_pnl = max_profit_usd
  max_profit_price = (max_profit_usd - realized_pnl - constant) / net_lots

For max_loss:
  P × net_lots + constant + realized_pnl = -max_loss_usd
  max_loss_price = (-max_loss_usd - realized_pnl - constant) / net_lots
```

### 3.4 Edge Case: Fully Hedged (net_lots ≈ 0)

If buy lots ≈ sell lots, PnL is fixed regardless of price movement:

```
fixed_pnl = constant + realized_pnl
```

- If fixed_pnl >= max_profit → trigger immediately
- If fixed_pnl <= -max_loss → trigger immediately
- Otherwise → PnL can never reach either threshold from price movement alone; wait for a position to close and change the composition

### 3.5 Directionality of Liquidation Prices

The direction depends on net exposure:

- **net_lots > 0** (more buys): max_profit_price is ABOVE current, max_loss_price is BELOW
- **net_lots < 0** (more sells): max_profit_price is BELOW current, max_loss_price is ABOVE

The implementation must handle both directions correctly when comparing on each tick.

### 3.6 Realized PnL Tracking

When a position closes (TP or SL hit detected), we calculate its realized PnL:

```python
if direction == "buy":
    realized = (close_price - entry_price) * lot_size
else:  # sell
    realized = (entry_price - close_price) * lot_size

state.realized_pnl += realized
```

The close_price is the TP or SL level (whichever was hit), determined by the touch flag system.

### 3.7 When to Recalculate

| Event | What Changes | Recalculate? |
|-------|-------------|-------------|
| Both atomic fires complete | All positions known | YES — initial calculation |
| Single Buy opens | New buy position added | YES |
| Any position closes (TP/SL) | realized_pnl increases, open positions decrease | YES |
| Tick arrives | Nothing structural changes | NO — just compare price vs already-calculated levels |

---

## 4. CONFIG STRUCTURE

### 4.1 New Default Config

```python
DEFAULT_SYMBOL_CONFIG = {
    "enabled": False,
    "grid_distance": 50.0,       # Pips between atomic fires
    "tp_pips": 150.0,            # Take profit distance (applied to all positions)
    "sl_pips": 200.0,            # Stop loss distance (applied to all positions)
    "bx_lot": 0.01,              # Lot size for initial Buy (Pair X)
    "sy_lot": 0.01,              # Lot size for initial Sell (Pair Y)
    "sx_lot": 0.01,              # Lot size for completing Sell (Pair X)
    "by_lot": 0.01,              # Lot size for completing Buy (Pair Y)
    "single_buy_lot": 0.01,      # Lot size for recovery Buy on first TP
    "max_profit_usd": 100.0,     # Max profit in $ before nuclear reset
    "max_loss_usd": 50.0         # Max loss in $ before nuclear reset
}
```

### 4.2 Removed Fields (from old grid strategy)

```
lot_sizes[]          — replaced by 5 named lot sizes
max_positions        — no longer needed (fixed 4+1 positions)
max_pairs            — no longer needed (always 2 pairs)
hedge_enabled        — no hedging in new strategy
hedge_lot_size       — no hedging
buy_stop_tp          — replaced by single tp_pips
buy_stop_sl          — replaced by single sl_pips
sell_stop_tp         — replaced by single tp_pips
sell_stop_sl         — replaced by single sl_pips
spread               — renamed to grid_distance
```

### 4.3 TP/SL Application Logic

The single `tp_pips` and `sl_pips` values are applied directionally:

| Position | TP | SL |
|----------|----|----|
| BUY | entry + tp_pips | entry - sl_pips |
| SELL | entry - tp_pips | entry + sl_pips |

The user configures these values knowing that for their grid setup, Bx's SL will equal Sx's TP level (this is their responsibility, not the algorithm's).

---

## 5. STATE MACHINE — DETAILED

### 5.1 Phase Diagram

```
                    ┌─────────────────────────────────────────────────┐
                    │                                                 │
                    ▼                                                 │
    START ──► FIRST_FIRE ──► AWAITING_SECOND ──► PAIRS_COMPLETE ──► RESETTING
                  │                                    │              ▲
                  │                                    ▼              │
                  │                               MONITORING ────────┘
                  │
                  └──── (on_external_tick processes all phases) ──────┘
```

### 5.2 Phase Definitions

**IDLE**

- Engine exists but not running
- No positions open
- Waiting for `start()` call from UI/orchestrator

**FIRST_FIRE**

- Entered when `start()` is called
- Immediately opens Bx + Sy atomically
- Records `start_price`
- Transitions to AWAITING_SECOND on same tick (no waiting)

**AWAITING_SECOND**

- Monitoring every tick for `price >= start_price + grid_distance` OR `price <= start_price - grid_distance`
- When threshold hit: opens Sx + By atomically
- Sets `pairs_complete = True`
- Calculates initial liquidation prices
- Transitions to PAIRS_COMPLETE

**PAIRS_COMPLETE**

- All 4 positions are open
- Monitoring:
  - Position drops (TP/SL hits) via `_check_position_drops()`
  - Liquidation prices via `_check_liquidation_prices()`
  - Touch flags updating via `_update_touch_flags()`
- On first TP/SL:
  - Exception → RESETTING
  - Normal → opens single Buy, transitions to MONITORING

**MONITORING**

- Same monitoring as PAIRS_COMPLETE but `first_tp_handled = True`
- Single Buy is now open (up to 5 positions total)
- Continues monitoring position drops + liquidation prices
- Any liquidation threshold hit → RESETTING

**RESETTING**

- Close ALL open positions for this symbol
- Clear all state (ticket_map, touch_flags, realized_pnl)
- Increment `cycle_count`
- Auto-restart: call `start()` → FIRST_FIRE at current price

### 5.3 Tick Processing Order

On every tick, the engine processes in this exact order:

```
1. Skip if IDLE or RESETTING
2. If FIRST_FIRE → execute first atomic fire, transition
3. If AWAITING_SECOND → check grid distance threshold
4. If PAIRS_COMPLETE or MONITORING:
   a. Update touch flags (latch TP/SL proximity)
   b. Check position drops (detect closed positions)
   c. Check liquidation prices (compare vs thresholds)
5. Save state (periodic, not every tick)
```

---

## 6. IMPLEMENTATION — FILE BY FILE

### 6.1 NEW: `core/engine/pair_strategy_engine.py`

This replaces the 5300+ line `symbol_engine.py` with a ~500-800 line focused engine.

#### Data Model

```python
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional
import asyncio
import json
import time
import logging
import MetaTrader5 as mt5

@dataclass
class StrategyState:
    """Complete state for one symbol's strategy execution"""
    phase: str = "IDLE"
    start_price: float = 0.0

    # Pair X positions
    bx_ticket: int = 0
    bx_entry: float = 0.0
    sx_ticket: int = 0
    sx_entry: float = 0.0

    # Pair Y positions
    sy_ticket: int = 0
    sy_entry: float = 0.0
    by_ticket: int = 0
    by_entry: float = 0.0

    # Single Buy (recovery)
    single_buy_ticket: int = 0
    single_buy_entry: float = 0.0

    # PnL tracking
    realized_pnl: float = 0.0
    max_profit_price: float = 0.0
    max_loss_price: float = 0.0

    # Flags
    pairs_complete: bool = False
    first_tp_handled: bool = False
    cycle_count: int = 0
```

#### Class Structure — All Methods

```python
class PairStrategyEngine:

    def __init__(self, config_manager, symbol: str, session_logger=None):
        """
        Initialize engine for one symbol.

        Args:
            config_manager: ConfigManager instance for reading per-symbol config
            symbol: MT5 symbol name (e.g., "FX Vol 20")
            session_logger: Optional session logger for group/activity logging
        """

    # ========================
    # CONFIG PROPERTY ACCESSORS
    # ========================

    @property
    def config(self) -> dict:
        """Get symbol-specific config from config manager"""

    @property
    def grid_distance(self) -> float:
        """Pips between start and second atomic fire"""
        return float(self.config.get('grid_distance', 50.0))

    @property
    def tp_pips(self) -> float:
        return float(self.config.get('tp_pips', 150.0))

    @property
    def sl_pips(self) -> float:
        return float(self.config.get('sl_pips', 200.0))

    @property
    def bx_lot(self) -> float:
        return float(self.config.get('bx_lot', 0.01))

    @property
    def sy_lot(self) -> float:
        return float(self.config.get('sy_lot', 0.01))

    @property
    def sx_lot(self) -> float:
        return float(self.config.get('sx_lot', 0.01))

    @property
    def by_lot(self) -> float:
        return float(self.config.get('by_lot', 0.01))

    @property
    def single_buy_lot(self) -> float:
        return float(self.config.get('single_buy_lot', 0.01))

    @property
    def max_profit_usd(self) -> float:
        return float(self.config.get('max_profit_usd', 100.0))

    @property
    def max_loss_usd(self) -> float:
        return float(self.config.get('max_loss_usd', 50.0))

    # ========================
    # LIFECYCLE
    # ========================

    async def start(self):
        """
        Called when user clicks Start. Fires first atomic pair.

        Flow:
        1. Get current tick
        2. Open Bx (buy at ask) with bx_lot
        3. Open Sy (sell at bid) with sy_lot
        4. Record start_price = ask
        5. Store tickets in state + ticket_map
        6. Set phase = AWAITING_SECOND
        7. Save state

        If either order fails, log error but continue with what succeeded.
        """

    async def stop(self):
        """
        Graceful stop — stop monitoring but don't close positions.
        Sets self.running = False, phase = IDLE.
        """

    async def terminate(self):
        """
        Nuclear reset — close ALL positions for this symbol immediately.

        Flow:
        1. Get all MT5 positions for this symbol
        2. Close each one via _close_position()
        3. Clear ticket_map, touch_flags
        4. Reset state to defaults
        5. Set phase = IDLE, running = False
        """

    # ========================
    # TICK HANDLER (MAIN LOOP)
    # ========================

    async def on_external_tick(self, tick_data: dict):
        """
        Called by orchestrator on every tick. Routes to phase handler.

        Args:
            tick_data: {"ask": float, "bid": float, "positions_count": int}

        Flow:
        1. Extract ask, bid from tick_data
        2. Acquire execution_lock (prevent concurrent processing)
        3. Route to phase handler:
           - IDLE: return (do nothing)
           - FIRST_FIRE: should not happen (start() handles this synchronously)
           - AWAITING_SECOND: check grid distance
           - PAIRS_COMPLETE / MONITORING:
             a. _update_touch_flags(ask, bid)
             b. _check_position_drops(ask, bid)
             c. _check_liquidation_prices(ask, bid)
        4. Release lock
        """

    # ========================
    # PHASE HANDLERS
    # ========================

    async def _handle_awaiting_second(self, ask: float, bid: float):
        """
        Monitor for grid distance reached.

        Trigger condition:
          ask >= start_price + grid_distance  (price went UP)
          OR bid <= start_price - grid_distance  (price went DOWN)

        When triggered:
        1. Open Sx (sell at bid) with sx_lot
        2. Open By (buy at ask) with by_lot
        3. Store tickets in state + ticket_map
        4. Set pairs_complete = True
        5. Calculate initial liquidation prices
        6. Set phase = PAIRS_COMPLETE
        7. Save state
        """

    # ========================
    # MT5 ORDER EXECUTION
    # ========================

    async def _execute_market_order(self, direction: str, lot_size: float,
                                      leg_name: str) -> tuple:
        """
        Send a market order to MT5. Returns (ticket, entry_price) or (0, 0.0).

        Args:
            direction: "buy" or "sell"
            lot_size: Position size
            leg_name: Label for logging (e.g., "Bx", "Sy", "Sx", "By", "SingleBuy")

        Flow:
        1. Get current tick
        2. Determine exec_price: ask for buy, bid for sell
        3. Calculate TP/SL from config:
           - BUY: tp = exec_price + tp_pips, sl = exec_price - sl_pips
           - SELL: tp = exec_price - tp_pips, sl = exec_price + sl_pips
        4. Validate stops against broker minimums (symbol_info.trade_stops_level)
           - Adjust TP/SL if they violate minimum distance
        5. Build MT5 request:
           {
             action: TRADE_ACTION_DEAL,
             symbol: self.symbol,
             volume: lot_size,
             type: ORDER_TYPE_BUY or ORDER_TYPE_SELL,
             price: exec_price,
             sl: sl,
             tp: tp,
             magic: self.magic_number,
             comment: f"{leg_name} C{state.cycle_count}",
             type_time: ORDER_TIME_GTC,
             type_filling: ORDER_FILLING_FOK,
             deviation: 200
           }
        6. Send via mt5.order_send()
        7. On success:
           - Find position ticket from mt5.positions_get()
           - Add to ticket_map: ticket → {leg: leg_name, direction, entry, tp, sl, lot}
           - Initialize touch flags: {tp_touched: False, sl_touched: False}
           - Save ticket to persistence
           - Return (ticket, exec_price)
        8. On failure:
           - Log error with retcode and comment
           - Return (0, 0.0)
        """

    def _close_position(self, ticket: int) -> bool:
        """
        Close a single MT5 position by ticket.

        Flow:
        1. Get position info from mt5.positions_get(ticket=ticket)
        2. If not found, return False (already closed)
        3. Build close request:
           - type = SELL if position is BUY, BUY if position is SELL
           - price = bid for closing buys, ask for closing sells
           - volume = position volume
           - deviation = 50
        4. Send via mt5.order_send()
        5. Return True on success, False on failure
        """

    # ========================
    # TP/SL DETECTION SYSTEM
    # ========================

    def _update_touch_flags(self, ask: float, bid: float):
        """
        Latch touch flags when price crosses TP/SL levels.
        Called on every tick BEFORE position drop check.

        For each tracked ticket in ticket_map:
          If direction == "buy":
            if bid >= tp_price → set tp_touched = True
            if bid <= sl_price → set sl_touched = True
          If direction == "sell":
            if ask <= tp_price → set tp_touched = True
            if ask >= sl_price → set sl_touched = True

        These flags are "latched" — once True, never reset to False.
        This allows deterministic TP/SL classification even if the
        position closes between ticks.
        """

    async def _check_position_drops(self, ask: float, bid: float):
        """
        Detect positions that have been closed by MT5 (TP/SL hit).

        Flow:
        1. Get all live positions: mt5.positions_get(symbol=self.symbol)
        2. Build set of current_tickets
        3. Compare vs ticket_map keys → find dropped_tickets
        4. For each dropped ticket:
           a. Get info from ticket_map (leg, direction, entry, tp, sl, lot)
           b. Classify TP vs SL:
              - If tp_touched → it was TP, close_price = tp
              - If sl_touched → it was SL, close_price = sl
              - Fallback: infer from distance (bid/ask vs tp vs sl)
           c. Calculate realized PnL:
              - BUY: (close_price - entry) * lot
              - SELL: (entry - close_price) * lot
           d. Add to state.realized_pnl
           e. Clear ticket from state (set appropriate ticket field to 0)
           f. Remove from ticket_map and touch_flags
           g. Handle first TP logic (see below)
           h. Recalculate liquidation prices
        5. Save state if any drops detected
        """

    async def _handle_first_tp(self, close_price: float, ask: float, bid: float):
        """
        Handle the first TP/SL hit across all positions.
        Called only once (guarded by first_tp_handled flag).

        Flow:
        1. If first_tp_handled → return (already handled)
        2. Set first_tp_handled = True
        3. EXCEPTION CHECK:
           If pairs_complete AND close_price >= start_price:
             → Nuclear reset + auto-restart
             → Return (no single buy)
        4. NORMAL PATH:
           → Open single Buy at current ask with single_buy_lot
           → Store ticket in state.single_buy_ticket
           → Set phase = MONITORING
           → Recalculate liquidation prices
        """

    # ========================
    # LIQUIDATION PRICE SYSTEM
    # ========================

    def _calculate_liquidation_prices(self):
        """
        Calculate the exact prices where max profit/max loss are hit.

        Called when position composition changes (not on every tick).

        Algorithm:
        1. Collect all OPEN positions from state:
           - Check each ticket field (bx_ticket, sx_ticket, etc.)
           - If ticket > 0, position is open
           - Get entry and lot from state fields

        2. Calculate net_lots and constant:
           net_lots = 0.0
           constant = 0.0
           For each open position:
             if direction == "buy":
               net_lots += lot
               constant -= entry * lot
             else:  # sell
               net_lots -= lot
               constant += entry * lot

        3. Handle fully hedged (net_lots ≈ 0):
           fixed_pnl = constant + state.realized_pnl
           if fixed_pnl >= max_profit_usd:
             state.max_profit_price = 0  (immediate trigger)
             state.max_loss_price = float('inf')
           elif fixed_pnl <= -max_loss_usd:
             state.max_loss_price = 0  (immediate trigger)
             state.max_profit_price = float('inf')
           else:
             Both = float('inf')  (unreachable by price movement)
           return

        4. Solve linear equations:
           state.max_profit_price = (max_profit_usd - state.realized_pnl - constant) / net_lots
           state.max_loss_price = (-max_loss_usd - state.realized_pnl - constant) / net_lots

        5. Log the calculated levels for debugging.
        """

    async def _check_liquidation_prices(self, ask: float, bid: float):
        """
        Simple price comparison on every tick.

        Uses mid price = (ask + bid) / 2 for comparison.

        Must handle BOTH directions of net exposure:

        If net_lots > 0 (long bias):
          max_profit_price is ABOVE current → check mid >= max_profit_price
          max_loss_price is BELOW current → check mid <= max_loss_price

        If net_lots < 0 (short bias):
          max_profit_price is BELOW current → check mid <= max_profit_price
          max_loss_price is ABOVE current → check mid >= max_loss_price

        Simplified universal check:
          profit_hit = (net_lots > 0 and mid >= max_profit_price) or
                       (net_lots < 0 and mid <= max_profit_price)
          loss_hit =   (net_lots > 0 and mid <= max_loss_price) or
                       (net_lots < 0 and mid >= max_loss_price)

        OR even simpler — since the formula is linear:
          Calculate actual PnL at current mid:
            current_pnl = mid * net_lots + constant + realized_pnl
          if current_pnl >= max_profit_usd → profit hit
          if current_pnl <= -max_loss_usd → loss hit

        The second approach (calculate actual PnL) is simpler and more robust.
        It avoids directionality issues entirely.

        On hit: await _nuclear_reset_and_restart()
        """

    # ========================
    # NUCLEAR RESET
    # ========================

    async def _nuclear_reset_and_restart(self):
        """
        Close everything, reset state, restart immediately.

        Flow:
        1. Set phase = RESETTING
        2. Log the reset event (cycle_count, realized_pnl, reason)
        3. Get ALL positions for this symbol from MT5
        4. Close each via _close_position()
        5. Clear ticket_map and ticket_touch_flags
        6. Preserve cycle_count (increment it)
        7. Reset all other state fields to defaults
        8. Immediately call start() → opens new Bx + Sy at current price
        9. Phase transitions to AWAITING_SECOND
        """

    # ========================
    # STATE PERSISTENCE
    # ========================

    async def save_state(self):
        """
        Persist current state to SQLite.

        Serializes StrategyState to JSON + saves ticket_map and touch_flags.
        Called after significant events (fires, TP/SL hits, resets).
        NOT called on every tick (too expensive).
        """

    async def load_state(self):
        """
        Restore state from SQLite on startup.

        Loads StrategyState, ticket_map, and touch_flags.
        Resumes from wherever the engine was before shutdown.
        """

    # ========================
    # HELPERS
    # ========================

    def get_broker_spread(self) -> float:
        """Get current bid-ask spread from MT5"""
        tick = mt5.symbol_info_tick(self.symbol)
        if tick and tick.ask > 0 and tick.bid > 0:
            return tick.ask - tick.bid
        return 0.0

    def _get_open_positions_from_state(self) -> list:
        """
        Return list of open positions from state fields.
        Each entry: (direction, entry, lot, ticket_field_name)

        Checks: bx_ticket, sx_ticket, sy_ticket, by_ticket, single_buy_ticket
        Only includes positions where ticket > 0.
        """

    def _clear_ticket_from_state(self, ticket: int):
        """
        Find which state field holds this ticket and clear it.

        Checks all 5 ticket fields (bx, sx, sy, by, single_buy).
        Sets the matching ticket field to 0 and entry to 0.0.
        """

    def _log_activity(self, event_type: str, message: str):
        """Log to activity log file"""

    def get_status(self) -> dict:
        """
        Return status dict for API/UI polling.

        Returns:
        {
            "phase": str,
            "cycle_count": int,
            "start_price": float,
            "pairs_complete": bool,
            "first_tp_handled": bool,
            "open_positions": int,
            "realized_pnl": float,
            "max_profit_price": float,
            "max_loss_price": float,
            "positions": {
                "bx": {"ticket": int, "entry": float, "lot": float},
                "sy": {...}, "sx": {...}, "by": {...}, "single_buy": {...}
            }
        }
        """
```

### 6.2 MODIFY: `static/index.html`

#### Remove These UI Sections

1. **Max Pairs dropdown** (currently renders 1,3,5,7,9 options)
2. **Max Positions slider** (currently 1-20, controls lot_sizes count)
3. **Lot Sizes dynamic grid** (currently N inputs based on max_positions)
4. **Hedge Settings** (hedge_enabled toggle + hedge_lot_size input)

#### Add These UI Sections

Replace the removed sections with:

```html
<!-- Grid & TP/SL Settings -->
<div class="grid grid-cols-3 gap-2">
    <div>
        <label class="block text-[10px] text-cyan-400 mb-1">Grid Distance (pips)</label>
        <input type="number" id="grid_dist_${safeId}" step="1" value="${gridDist}"
            class="w-full p-1.5 text-sm rounded bg-slate-900 border border-cyan-700/50">
    </div>
    <div>
        <label class="block text-[10px] text-green-400 mb-1">TP (pips)</label>
        <input type="number" id="tp_pips_${safeId}" step="1" value="${tpPips}"
            class="w-full p-1.5 text-sm rounded bg-slate-900 border border-green-700/50">
    </div>
    <div>
        <label class="block text-[10px] text-red-400 mb-1">SL (pips)</label>
        <input type="number" id="sl_pips_${safeId}" step="1" value="${slPips}"
            class="w-full p-1.5 text-sm rounded bg-slate-900 border border-red-700/50">
    </div>
</div>

<!-- Position Lot Sizes -->
<div class="border-t border-slate-700 pt-2 mt-2">
    <label class="block text-[10px] text-yellow-400 mb-1">Position Lot Sizes</label>
    <div class="grid grid-cols-2 gap-2">
        <div>
            <label class="block text-[8px] text-slate-400">Bx (Initial Buy)</label>
            <input type="number" id="bx_lot_${safeId}" step="0.01" value="${bxLot}"
                class="w-full p-1.5 text-sm rounded bg-slate-900 border border-blue-700/50">
        </div>
        <div>
            <label class="block text-[8px] text-slate-400">Sy (Initial Sell)</label>
            <input type="number" id="sy_lot_${safeId}" step="0.01" value="${syLot}"
                class="w-full p-1.5 text-sm rounded bg-slate-900 border border-orange-700/50">
        </div>
        <div>
            <label class="block text-[8px] text-slate-400">Sx (Completing Sell)</label>
            <input type="number" id="sx_lot_${safeId}" step="0.01" value="${sxLot}"
                class="w-full p-1.5 text-sm rounded bg-slate-900 border border-orange-700/50">
        </div>
        <div>
            <label class="block text-[8px] text-slate-400">By (Completing Buy)</label>
            <input type="number" id="by_lot_${safeId}" step="0.01" value="${byLot}"
                class="w-full p-1.5 text-sm rounded bg-slate-900 border border-blue-700/50">
        </div>
    </div>
</div>

<!-- Recovery Buy -->
<div class="border-t border-slate-700 pt-2 mt-2">
    <label class="block text-[10px] text-purple-400 mb-1">Recovery (Single Buy on TP)</label>
    <input type="number" id="single_buy_lot_${safeId}" step="0.01" value="${singleBuyLot}"
        class="w-full p-1.5 text-sm rounded bg-slate-900 border border-purple-700/50">
</div>

<!-- Risk Management -->
<div class="grid grid-cols-2 gap-2 border-t border-slate-700 pt-2 mt-2">
    <div>
        <label class="block text-[10px] text-green-400 mb-1">Max Profit ($)</label>
        <input type="number" id="max_profit_${safeId}" step="1" value="${maxProfit}"
            class="w-full p-1.5 text-sm rounded bg-slate-900 border border-green-700/50">
    </div>
    <div>
        <label class="block text-[10px] text-red-400 mb-1">Max Loss ($)</label>
        <input type="number" id="max_loss_${safeId}" step="1" value="${maxLoss}"
            class="w-full p-1.5 text-sm rounded bg-slate-900 border border-red-700/50">
    </div>
</div>
```

#### Update `updateConfig()` Collection

Replace lot_sizes/hedge collection with:

```javascript
// Collect new config fields
payload.symbols[symbol] = {
    enabled: true,
    grid_distance: parseFloat(document.getElementById(`grid_dist_${safeId}`)?.value) || 50.0,
    tp_pips: parseFloat(document.getElementById(`tp_pips_${safeId}`)?.value) || 150.0,
    sl_pips: parseFloat(document.getElementById(`sl_pips_${safeId}`)?.value) || 200.0,
    bx_lot: parseFloat(document.getElementById(`bx_lot_${safeId}`)?.value) || 0.01,
    sy_lot: parseFloat(document.getElementById(`sy_lot_${safeId}`)?.value) || 0.01,
    sx_lot: parseFloat(document.getElementById(`sx_lot_${safeId}`)?.value) || 0.01,
    by_lot: parseFloat(document.getElementById(`by_lot_${safeId}`)?.value) || 0.01,
    single_buy_lot: parseFloat(document.getElementById(`single_buy_lot_${safeId}`)?.value) || 0.01,
    max_profit_usd: parseFloat(document.getElementById(`max_profit_${safeId}`)?.value) || 100.0,
    max_loss_usd: parseFloat(document.getElementById(`max_loss_${safeId}`)?.value) || 50.0
};
```

### 6.3 MODIFY: `core/config_manager.py`

- Replace `DEFAULT_SYMBOL_CONFIG` dict with new fields (see Section 4.1)
- Update `update_config()` validation:
  - All lot sizes must be > 0 (default to 0.01)
  - grid_distance must be > 0
  - tp_pips and sl_pips must be > 0
  - max_profit_usd and max_loss_usd must be > 0
- Remove old validation for max_pairs, max_positions, lot_sizes length, hedge fields

### 6.4 MODIFY: `core/strategy_orchestrator.py`

Minimal change — swap the engine class:

```python
# Old:
from core.engine.symbol_engine import SymbolEngine as GridStrategy

# New:
from core.engine.pair_strategy_engine import PairStrategyEngine as GridStrategy
```

The orchestrator's interface (start, stop, terminate, on_external_tick, get_status) remains the same — the new engine implements the same methods.

### 6.5 MODIFY: `core/persistence/repository.py`

Simplify the schema. Replace complex grid_pairs table with a single strategy_state table:

```sql
CREATE TABLE IF NOT EXISTS strategy_state (
    symbol TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,        -- JSON serialized StrategyState
    ticket_map_json TEXT DEFAULT '{}', -- JSON serialized ticket_map
    touch_flags_json TEXT DEFAULT '{}', -- JSON serialized touch_flags
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Keep tickets table for recovery
CREATE TABLE IF NOT EXISTS tickets (
    ticket INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    leg TEXT NOT NULL,               -- "Bx", "Sy", "Sx", "By", "SingleBuy"
    direction TEXT NOT NULL,         -- "buy" or "sell"
    entry_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    sl_price REAL NOT NULL,
    lot_size REAL NOT NULL,
    cycle_count INTEGER DEFAULT 0
);
```

### 6.6 MODIFY: `api/server.py`

Minimal — the FastAPI `ConfigUpdate` model uses a flexible dict structure, so new fields are accepted automatically. May need to verify that the `get_status` endpoint returns the new status format correctly.

### 6.7 DELETE: `core/engine/symbol_engine.py`

No longer needed. The entire file is replaced by `pair_strategy_engine.py`.

Also clean up any imports of `SymbolEngine`, `GridPair`, etc. in other files.

---

## 7. EDGE CASES & ERROR HANDLING

### 7.1 Order Failure During Atomic Fire

If one order succeeds but the other fails:

- Log the failure
- The successful order remains open
- Retry the failed order on the next tick (or a few retries with delay)
- If retry exhausts: log error, keep partial state, let user intervene

### 7.2 Bot Restart Mid-Cycle

State is persisted to SQLite. On restart:

- Load state from DB
- Rebuild ticket_map from tickets table
- Resume from the saved phase
- Re-verify open positions against MT5 (reconcile)

### 7.3 Position Closed Between Ticks

Touch flag latching handles this. Even if a position closes between tick polls, the flags were latched during prior ticks when price was near TP/SL levels.

### 7.4 All Positions Close Before Liquidation

If all positions close via TP/SL before max profit/max loss is reached:

- realized_pnl contains all closed P&L
- No open positions remain
- net_lots = 0, fixed_pnl = realized_pnl
- If it exceeds threshold → reset
- If not → this is a dead state; should trigger nuclear reset anyway since there's nothing left to monitor

### 7.5 MT5 Connection Loss

The existing TradingEngine handles reconnection (health checks every 100 ticks). The strategy engine just won't receive ticks during disconnection. On reconnection, it resumes normally.

### 7.6 Max Profit/Max Loss = 0 or Very Small

If configured too small, the bot might immediately trigger on the first atomic fire's spread. Validation should enforce minimums, or the liquidation calculation should only activate after pairs_complete.

---

## 8. LOGGING

### Log Files

- `logs/trading_activity.log` — Major events (fires, TP hits, resets, liquidation)
- `logs/pair_strategy_debug.log` — Tick-level debug (phase, prices, touch flags)
- Console `print()` — Key events with `[LOCKED]`, `[FIRE]`, `[TP]`, `[RESET]` prefixes

### Key Log Messages

```
[FIRE] Cycle 0: Bx OPEN @ 1000.00 (lot=0.10) + Sy OPEN @ 1000.00 (lot=0.10) | start_price=1000.00
[FIRE] Cycle 0: Sx OPEN @ 1050.00 (lot=0.05) + By OPEN @ 1050.00 (lot=0.05) | pairs_complete
[LIQUIDATION] Calculated: profit_price=1250.00, loss_price=850.00 | net_lots=0.05, constant=-50.0, realized=0.0
[TP] Bx hit TP @ 1150.00 | realized_pnl=+15.00 | EXCEPTION: price >= start → RESET
[TP] Sy hit TP @ 850.00 | realized_pnl=+15.00 | Opening Single Buy
[SINGLE_BUY] OPEN @ 852.00 (lot=0.03) | TP=1002.00, SL=652.00
[LIQUIDATION] Recalculated: profit_price=1180.00, loss_price=750.00 | realized=15.00
[RESET] Max profit hit @ 1180.50 | total_pnl=$100.25 | cycle=0 → restarting
[FIRE] Cycle 1: Bx OPEN @ 1180.50 ...
```

---

## 9. FILES SUMMARY

| File | Action | Lines (est.) | Description |
|------|--------|-------------|-------------|
| `core/engine/pair_strategy_engine.py` | **CREATE** | ~600-800 | New strategy engine |
| `core/engine/symbol_engine.py` | **DELETE** | -5300 | Old grid engine |
| `static/index.html` | **MODIFY** | ~100 changed | New UI config sections |
| `core/config_manager.py` | **MODIFY** | ~30 changed | New defaults + validation |
| `core/strategy_orchestrator.py` | **MODIFY** | ~5 changed | Swap import |
| `core/persistence/repository.py` | **MODIFY** | ~50 changed | Simplified schema |
| `api/server.py` | **MODIFY** | ~5 changed | Minor if needed |

---

## 10. VERIFICATION CHECKLIST

1. **First fire**: Start → Bx + Sy open at same price, correct lots, correct TP/SL
2. **Second fire**: Price moves grid_distance → Sx + By open, correct lots
3. **Liquidation calc**: Check logged profit/loss prices match hand-calculated values
4. **TP normal**: First TP below start_price → single Buy opens with correct lot
5. **TP exception**: First TP at/above start_price → nuclear reset, no single Buy
6. **Liquidation hit**: Price reaches max_profit_price → all positions close, restart
7. **Realized PnL**: After position close, recalculated liquidation prices shift correctly
8. **Full hedged**: Equal buy/sell lots → PnL is fixed, verify immediate trigger if threshold met
9. **Restart**: After nuclear reset, new Bx + Sy fire at current price, cycle_count increments
10. **Persistence**: Kill bot mid-cycle, restart → resumes from saved phase
11. **UI**: Config saves correctly, loads correctly, all 10 fields present
