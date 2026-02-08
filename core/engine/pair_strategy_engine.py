"""
Pair Strategy Engine

Implements the paired-position trading strategy:
1. First atomic fire: Open Bx (buy) + Sy (sell) at start price
2. Wait for grid_distance pips movement
3. Second atomic fire: Open Sx (sell) + By (buy), record price & calculate triggers
4. Math-based triggers (PAIRS_COMPLETE phase):
   a. Single fire trigger: price reaches 3*grid_distance past second fire -> fire recovery
   b. Protection trigger: price reverses past protection_distance -> nuclear reset
5. After single fire: force-close the opposing pair (spread safety)
6. All positions closed: auto-restart cycle (or stop if graceful)
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Tuple
import asyncio
import json
import time
import logging
import MetaTrader5 as mt5
from datetime import datetime

from core.engine.activity_logger import ActivityLogger

logger = logging.getLogger("pair_strategy")


@dataclass
class StrategyState:
    """Complete state for one symbol's strategy execution"""
    phase: str = "IDLE"  # IDLE, AWAITING_SECOND, PAIRS_COMPLETE, MONITORING, RESETTING
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

    # Single Fire
    single_fire_ticket: int = 0
    single_fire_entry: float = 0.0
    single_fire_dir: str = ""  # Actual direction the single fire was opened with

    # Second atomic fire reference
    second_fire_price: float = 0.0
    location: str = ""  # "UP" or "DOWN"

    # Math-based trigger prices (calculated when second fire opens)
    single_fire_trigger_price: float = 0.0  # Price level that triggers single fire
    protection_trigger_price: float = 0.0   # Price level that triggers nuclear reset

    # PnL tracking
    realized_pnl: float = 0.0

    # Flags
    pairs_complete: bool = False
    single_fire_executed: bool = False
    cycle_count: int = 0


class PairStrategyEngine:
    """
    Main strategy engine for a single symbol.

    Implements lifecycle: start(), stop(), terminate(), on_external_tick()
    """

    MAGIC_NUMBER = 123456

    def __init__(self, config_manager, symbol: str, user_id: str = "default", session_logger=None):
        self.config_manager = config_manager
        self.symbol = symbol
        self.user_id = user_id
        self.session_logger = session_logger

        # State
        self.state = StrategyState()
        self.running = False
        self.graceful_stop = False

        # Ticket tracking
        self.ticket_map: Dict[int, dict] = {}  # ticket -> {leg, direction, entry, tp, sl, lot}
        self.ticket_touch_flags: Dict[int, dict] = {}  # ticket -> {tp_touched, sl_touched}

        # Execution lock
        self.execution_lock = asyncio.Lock()

        # Activity logger (now wired to session logger too)
        self.activity_log = ActivityLogger(symbol, user_id, session_logger=session_logger)

        # Persistence
        self.db_path = f"db/pair_strategy_{user_id}.db"

    # ========================
    # CONFIG ACCESSORS
    # ========================

    @property
    def config(self) -> dict:
        """Get symbol-specific config"""
        return self.config_manager.get_symbol_config(self.symbol) or {}

    @property
    def grid_distance(self) -> float:
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
    def single_fire_lot(self) -> float:
        return float(self.config.get('single_fire_lot', 0.01))

    @property
    def single_fire_tp_pips(self) -> float:
        return float(self.config.get('single_fire_tp_pips', 150.0))

    @property
    def single_fire_sl_pips(self) -> float:
        return float(self.config.get('single_fire_sl_pips', 200.0))

    @property
    def protection_distance(self) -> float:
        return float(self.config.get('protection_distance', 100.0))

    # ========================
    # LIFECYCLE
    # ========================

    async def start(self):
        """
        Start the strategy - fires first atomic pair (Bx + Sy).
        """
        if self.running:
            return

        self.running = True
        self.graceful_stop = False

        # Get current tick
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.activity_log.log_error("Failed to get tick for start")
            return

        ask, bid = tick.ask, tick.bid
        self.state.start_price = ask

        self.activity_log.log_start(self.state.cycle_count, ask)
        self.activity_log.log_phase_transition("IDLE", "FIRST_FIRE")

        # First atomic fire: Bx + Sy
        bx_ticket, bx_entry = await self._execute_market_order("buy", self.bx_lot, "Bx")
        sy_ticket, sy_entry = await self._execute_market_order("sell", self.sy_lot, "Sy")

        if bx_ticket:
            self.state.bx_ticket = bx_ticket
            self.state.bx_entry = bx_entry
            self.activity_log.log_fire(
                self.state.cycle_count, "Bx", bx_entry, self.bx_lot,
                bx_entry + self.tp_pips, bx_entry - self.sl_pips, bx_ticket
            )

        if sy_ticket:
            self.state.sy_ticket = sy_ticket
            self.state.sy_entry = sy_entry
            self.activity_log.log_fire(
                self.state.cycle_count, "Sy", sy_entry, self.sy_lot,
                sy_entry - self.tp_pips, sy_entry + self.sl_pips, sy_ticket
            )

        # Transition to awaiting second
        self.state.phase = "AWAITING_SECOND"
        self.activity_log.log_phase_transition("FIRST_FIRE", "AWAITING_SECOND")

        await self.save_state()

    async def stop(self):
        """
        Graceful stop - sets flag to complete current cycle before fully stopping.
        When graceful_stop is True:
        - Allow current cycle to continue monitoring
        - When cycle ends (TP/SL/all closed), stop completely (no auto-restart)
        """
        if not self.running:
            return

        print(f"[STOP] {self.symbol}: Graceful stop initiated. Finishing current cycle...")
        self.graceful_stop = True
        self.activity_log.log_graceful_stop(self.state.cycle_count, "manual/timeout")

        # If we're in IDLE or have no positions, stop immediately
        open_positions = self._get_open_positions_from_state()
        if self.state.phase == "IDLE" or not open_positions:
            self.running = False
            self.activity_log.log_stop(self.state.cycle_count, "graceful_stop_immediate")
            await self.save_state()
            print(f"[STOP] {self.symbol}: No active positions - stopped immediately.")

    async def terminate(self):
        """
        Nuclear reset - close ALL positions for this symbol immediately.
        Resets all pair states. Does NOT restart.
        """
        print(f"[TERMINATE] {self.symbol}: Closing ALL positions immediately...")
        self.activity_log.log_info("TERMINATE: Closing all positions...")

        # Close all positions
        positions = mt5.positions_get(symbol=self.symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                if self._close_position(pos.ticket):
                    closed_count += 1
                else:
                    print(f"[ERROR] Failed to close position {pos.ticket}")

        print(f"[TERMINATE] {self.symbol}: Closed {closed_count} positions.")
        self.activity_log.log_info(f"TERMINATE: Closed {closed_count} positions")

        # Reset state completely
        self._reset_state()
        self.running = False
        self.graceful_stop = False
        self.state.phase = "IDLE"
        self.state.cycle_count = 0  # Full reset for nuclear terminate

        print(f"[SHUTDOWN] {self.symbol}: Grid engine stopped.")
        await self.save_state()
        print(f"[TERMINATE] {self.symbol}: Grid reset complete.")

    # ========================
    # TICK HANDLER
    # ========================

    async def on_external_tick(self, tick_data: dict):
        """
        Called by orchestrator on every tick. Routes to phase handler.
        """
        if not self.running or self.state.phase == "IDLE":
            return

        ask = tick_data.get('ask', 0)
        bid = tick_data.get('bid', 0)

        if ask <= 0 or bid <= 0:
            return

        async with self.execution_lock:
            # 1. Update touch flags FIRST
            self._update_touch_flags(ask, bid)

            # 2. Check position drops (cleanup/PnL only - no strategic TP logic)
            await self._check_position_drops(ask, bid)

            # 3. If single fire closed (TP/SL) -> nuclear reset all remaining
            if await self._check_single_fire_closed():
                return

            # 4. Check math-based triggers (single fire + protection)
            await self._check_math_triggers(ask, bid)

            # 5. Check if all positions are closed
            await self._check_all_positions_closed()

            # 6. Phase-specific logic
            if self.state.phase == "AWAITING_SECOND":
                await self._handle_awaiting_second(ask, bid)

    # ========================
    # PHASE HANDLERS
    # ========================

    async def _handle_awaiting_second(self, ask: float, bid: float):
        """
        Wait for grid distance to be reached, then fire second atomic pair.
        Records location (UP/DOWN) of second fire relative to first.
        """
        start = self.state.start_price

        # Check if grid distance reached (either direction)
        triggered_up = ask >= start + self.grid_distance
        triggered_down = bid <= start - self.grid_distance

        if not (triggered_up or triggered_down):
            return

        trigger_price = ask if triggered_up else bid

        # Record location and reference price of second atomic fire
        self.state.location = "UP" if triggered_up else "DOWN"
        self.state.second_fire_price = trigger_price
        print(f"[LOCATION] {self.symbol}: Second atomic fire location = {self.state.location} @ {trigger_price:.5f}")
        direction_word = "below" if self.state.location == "DOWN" else "above"
        self.activity_log.log_info(
            f"2nd pair opened {direction_word} the 1st pair (at price {trigger_price:.2f})"
        )

        # Calculate math-based trigger prices
        if self.state.location == "DOWN":
            self.state.single_fire_trigger_price = trigger_price - 3 * self.grid_distance
            self.state.protection_trigger_price = trigger_price + self.protection_distance
            sf_dir = "BUY"
            print(f"[TRIGGERS] {self.symbol}: SF trigger (BUY) @ bid <= {self.state.single_fire_trigger_price:.5f}, "
                  f"Protection @ ask >= {self.state.protection_trigger_price:.5f}")
        else:  # UP
            self.state.single_fire_trigger_price = trigger_price + 3 * self.grid_distance
            self.state.protection_trigger_price = trigger_price - self.protection_distance
            sf_dir = "SELL"
            print(f"[TRIGGERS] {self.symbol}: SF trigger (SELL) @ ask >= {self.state.single_fire_trigger_price:.5f}, "
                  f"Protection @ bid <= {self.state.protection_trigger_price:.5f}")

        self.activity_log.log_info(
            f"Recovery {sf_dir} will trigger at price {self.state.single_fire_trigger_price:.2f}  |  "
            f"Protection reset at price {self.state.protection_trigger_price:.2f}"
        )

        # Check if graceful stop is active - skip opening second pair
        if self.graceful_stop:
            self.activity_log.log_info(
                f"Grid distance reached at {trigger_price:.2f} but graceful stop is active — skipping 2nd pair"
            )
            # Transition directly to monitoring with just Bx+Sy
            self.state.pairs_complete = True
            self.state.phase = "PAIRS_COMPLETE"
            self.activity_log.log_phase_transition("AWAITING_SECOND", "PAIRS_COMPLETE (partial)")
            await self.save_state()
            return

        self.activity_log.log_second_fire(self.state.cycle_count, trigger_price)

        # Second atomic fire: Sx + By
        sx_ticket, sx_entry = await self._execute_market_order("sell", self.sx_lot, "Sx")
        by_ticket, by_entry = await self._execute_market_order("buy", self.by_lot, "By")

        if sx_ticket:
            self.state.sx_ticket = sx_ticket
            self.state.sx_entry = sx_entry
            self.activity_log.log_fire(
                self.state.cycle_count, "Sx", sx_entry, self.sx_lot,
                sx_entry - self.tp_pips, sx_entry + self.sl_pips, sx_ticket
            )

        if by_ticket:
            self.state.by_ticket = by_ticket
            self.state.by_entry = by_entry
            self.activity_log.log_fire(
                self.state.cycle_count, "By", by_entry, self.by_lot,
                by_entry + self.tp_pips, by_entry - self.sl_pips, by_ticket
            )

        # Both pairs now complete
        self.state.pairs_complete = True
        self.state.phase = "PAIRS_COMPLETE"
        self.activity_log.log_phase_transition("AWAITING_SECOND", "PAIRS_COMPLETE")

        await self.save_state()

    # ========================
    # MT5 ORDER EXECUTION
    # ========================

    async def _execute_market_order(self, direction: str, lot_size: float,
                                     leg_name: str, tp_pips: float = None,
                                     sl_pips: float = None) -> Tuple[int, float]:
        """
        Send a market order to MT5. Returns (ticket, entry_price) or (0, 0.0).
        Optional tp_pips/sl_pips override the default grid TP/SL.
        """
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            self.activity_log.log_error(f"No tick for {leg_name}")
            return 0, 0.0

        # Use overrides if provided, else defaults
        tp_pips_val = tp_pips if tp_pips is not None else self.tp_pips
        sl_pips_val = sl_pips if sl_pips is not None else self.sl_pips

        # Determine price and TP/SL
        if direction == "buy":
            exec_price = tick.ask
            tp = exec_price + tp_pips_val
            sl = exec_price - sl_pips_val
            order_type = mt5.ORDER_TYPE_BUY
            check_price = tick.bid # For stop level validation
        else:
            exec_price = tick.bid
            tp = exec_price - tp_pips_val
            sl = exec_price + sl_pips_val
            order_type = mt5.ORDER_TYPE_SELL
            check_price = tick.ask # For stop level validation

        # --- TRADE STOPS LEVEL SAFETY (from SymbolEngine) ---
        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info:
            point = symbol_info.point
            stops_level = max(symbol_info.trade_stops_level, 10) # Min 10 pts safety
            min_dist = stops_level * point

            if direction == "buy":
                if sl > check_price - min_dist:
                    sl = check_price - min_dist
                if tp < check_price + min_dist:
                    tp = check_price + min_dist
            else:
                if sl < check_price + min_dist:
                    sl = check_price + min_dist
                if tp > check_price - min_dist:
                    tp = check_price - min_dist

        # Snapshot existing tickets BEFORE opening (to find the new one after)
        positions_before = mt5.positions_get(symbol=self.symbol)
        existing_tickets = set(pos.ticket for pos in positions_before) if positions_before else set()

        # Build request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": exec_price,
            "sl": float(sl),
            "tp": float(tp),
            "magic": self.MAGIC_NUMBER,
            "comment": f"{leg_name} C{self.state.cycle_count}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "deviation": 200
        }

        # Send order
        result = mt5.order_send(request)

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error = mt5.last_error() if result is None else result.comment
            self.activity_log.log_error(f"{leg_name} order failed: {error}")
            return 0, 0.0

        # Get ticket from result
        ticket = result.order

        # Wait briefly for position to appear
        await asyncio.sleep(0.1)

        # Find the NEW position by comparing before/after snapshots
        positions_after = mt5.positions_get(symbol=self.symbol)
        actual_entry = exec_price
        actual_ticket = ticket

        if positions_after:
            # First: find the new ticket that didn't exist before
            for pos in positions_after:
                if pos.ticket not in existing_tickets:
                    actual_ticket = pos.ticket
                    actual_entry = pos.price_open
                    break
            else:
                # Fallback: match by order ticket only (no magic number match)
                for pos in positions_after:
                    if pos.ticket == ticket:
                        actual_ticket = pos.ticket
                        actual_entry = pos.price_open
                        break

        # Store in ticket_map
        self.ticket_map[actual_ticket] = {
            "leg": leg_name,
            "direction": direction,
            "entry": actual_entry,
            "tp": tp,
            "sl": sl,
            "lot": lot_size
        }

        # Initialize touch flags
        self.ticket_touch_flags[actual_ticket] = {
            "tp_touched": False,
            "sl_touched": False
        }

        return actual_ticket, actual_entry

    def _close_position(self, ticket: int) -> bool:
        """Close a single MT5 position by ticket."""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False  # Already closed

        pos = positions[0]

        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return False

        # Determine close parameters
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            close_price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            close_price = tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": close_price,
            "deviation": 50,
            "magic": self.MAGIC_NUMBER,
            "comment": "close",
            "type_filling": mt5.ORDER_FILLING_FOK
        }

        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


    # ========================
    # TP/SL DETECTION
    # ========================

    def _update_touch_flags(self, ask: float, bid: float):
        """
        Latch touch flags when price crosses TP/SL levels.
        Called EVERY tick BEFORE position drop check.
        """
        for ticket, info in list(self.ticket_map.items()):
            if not info:
                continue

            direction = info.get("direction", "")
            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)

            flags = self.ticket_touch_flags.get(ticket)
            if flags is None:
                flags = {"tp_touched": False, "sl_touched": False}
                self.ticket_touch_flags[ticket] = flags

            if direction == "buy":
                # BUY TP hit when bid >= tp_price
                if not flags['tp_touched'] and bid >= tp_price:
                    flags['tp_touched'] = True
                # BUY SL hit when bid <= sl_price
                if not flags['sl_touched'] and bid <= sl_price:
                    flags['sl_touched'] = True
            else:  # sell
                # SELL TP hit when ask <= tp_price
                if not flags['tp_touched'] and ask <= tp_price:
                    flags['tp_touched'] = True
                # SELL SL hit when ask >= sl_price
                if not flags['sl_touched'] and ask >= sl_price:
                    flags['sl_touched'] = True

    async def _check_position_drops(self, ask: float, bid: float):
        """
        Detect positions that have been closed by MT5 (TP/SL hit).
        """
        # Get current open tickets
        positions = mt5.positions_get(symbol=self.symbol)
        current_tickets = set()
        if positions:
            for pos in positions:
                current_tickets.add(pos.ticket)

        # Find dropped tickets
        tracked_tickets = set(self.ticket_map.keys())
        dropped = tracked_tickets - current_tickets

        for ticket in dropped:
            info = self.ticket_map.get(ticket)
            if not info:
                continue

            leg = info.get("leg", "")
            direction = info.get("direction", "")
            entry = info.get("entry", 0)
            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)
            lot = info.get("lot", 0)

            # Determine if TP or SL using touch flags
            flags = self.ticket_touch_flags.get(ticket, {})
            is_tp = flags.get("tp_touched", False)
            is_sl = flags.get("sl_touched", False)

            # Fallback: infer from current price distance
            if not is_tp and not is_sl:
                # Use side-aware price for inference
                check_price = bid if direction == "buy" else ask
                tp_dist = abs(check_price - tp_price)
                sl_dist = abs(check_price - sl_price)
                is_tp = tp_dist < sl_dist
                is_sl = not is_tp

                reason = "TP" if is_tp else "SL"
                print(f"[DROP-INFER] {self.symbol}: Ticket={ticket} {direction.upper()} "
                      f"-> {reason} (dist_tp={tp_dist:.5f}, dist_sl={sl_dist:.5f})")

            # Calculate realized PnL
            close_price = tp_price if is_tp else sl_price
            if direction == "buy":
                realized = (close_price - entry) * lot
            else:
                realized = (entry - close_price) * lot

            self.state.realized_pnl += realized

            # Log the event
            if is_tp:
                self.activity_log.log_tp_hit(ticket, leg, close_price, realized, "")
            else:
                self.activity_log.log_sl_hit(ticket, leg, close_price, realized)

            # Clear from tracking
            self._clear_ticket_from_state(ticket)
            del self.ticket_map[ticket]
            if ticket in self.ticket_touch_flags:
                del self.ticket_touch_flags[ticket]

            # NOTE: No strategic action on TP/SL - math triggers handle all decisions

        if dropped:
            await self.save_state()

    # ========================
    # MATH-BASED TRIGGERS
    # ========================

    async def _check_math_triggers(self, ask: float, bid: float):
        """
        Check math-based price triggers during PAIRS_COMPLETE phase.
        Two mutually exclusive exit paths:
        1. Single fire trigger: price moved 3*grid_distance past second fire -> recovery trade
        2. Protection trigger: price reversed past protection_distance -> nuclear reset
        """
        if self.state.phase != "PAIRS_COMPLETE":
            return
        if self.state.single_fire_executed:
            return
        if not self.state.second_fire_price:
            return

        if self.state.location == "DOWN":
            # Single fire trigger: bid falls to/below trigger -> BUY
            if bid <= self.state.single_fire_trigger_price:
                print(f"[MATH-TRIGGER] {self.symbol}: Single fire BUY triggered "
                      f"(bid {bid:.5f} <= {self.state.single_fire_trigger_price:.5f})")
                self.activity_log.log_info(
                    f"Price reached {bid:.2f} — placing Recovery BUY trade"
                )
                self.state.single_fire_executed = True
                await self._execute_single_fire(bid, "buy")
                # Force-close Pair X (Bx + Sx) - broker spread may have prevented TP/SL
                await self._force_close_pair("X")
                return

            # Protection trigger: ask rises to/above protection price -> nuclear reset
            if ask >= self.state.protection_trigger_price:
                print(f"[PROTECTION] {self.symbol}: Protection triggered "
                      f"(ask {ask:.5f} >= {self.state.protection_trigger_price:.5f})")
                self.activity_log.log_info(
                    f"Price reversed to {ask:.2f} — hit protection level. Closing all trades and restarting."
                )
                await self._nuclear_reset_and_restart("PROTECTION_DISTANCE", self.state.realized_pnl)
                return

        elif self.state.location == "UP":
            # Single fire trigger: ask rises to/above trigger -> SELL
            if ask >= self.state.single_fire_trigger_price:
                print(f"[MATH-TRIGGER] {self.symbol}: Single fire SELL triggered "
                      f"(ask {ask:.5f} >= {self.state.single_fire_trigger_price:.5f})")
                self.activity_log.log_info(
                    f"Price reached {ask:.2f} — placing Recovery SELL trade"
                )
                self.state.single_fire_executed = True
                await self._execute_single_fire(ask, "sell")
                # Force-close Pair Y (By + Sy) - broker spread may have prevented TP/SL
                await self._force_close_pair("Y")
                return

            # Protection trigger: bid falls to/below protection price -> nuclear reset
            if bid <= self.state.protection_trigger_price:
                print(f"[PROTECTION] {self.symbol}: Protection triggered "
                      f"(bid {bid:.5f} <= {self.state.protection_trigger_price:.5f})")
                self.activity_log.log_info(
                    f"Price reversed to {bid:.2f} — hit protection level. Closing all trades and restarting."
                )
                await self._nuclear_reset_and_restart("PROTECTION_DISTANCE", self.state.realized_pnl)
                return

    async def _force_close_pair(self, pair: str):
        """
        Force-close all positions in a pair (X or Y).
        Pair X = Bx (buy) + Sx (sell)
        Pair Y = Sy (sell) + By (buy)
        Called after single fire because broker spread may prevent TP/SL from filling.
        """
        if pair == "X":
            tickets_to_close = [
                ("bx", self.state.bx_ticket),
                ("sx", self.state.sx_ticket),
            ]
        elif pair == "Y":
            tickets_to_close = [
                ("sy", self.state.sy_ticket),
                ("by", self.state.by_ticket),
            ]
        else:
            return

        for leg_prefix, ticket in tickets_to_close:
            if ticket <= 0:
                continue  # Already closed

            print(f"[FORCE-CLOSE] {self.symbol}: Closing {leg_prefix.upper()} (ticket {ticket})")
            self.activity_log.log_info(f"Closing leftover {leg_prefix.upper()} trade (may not have closed due to spread)")

            if self._close_position(ticket):
                setattr(self.state, f"{leg_prefix}_ticket", 0)
                setattr(self.state, f"{leg_prefix}_entry", 0.0)
                if ticket in self.ticket_map:
                    del self.ticket_map[ticket]
                if ticket in self.ticket_touch_flags:
                    del self.ticket_touch_flags[ticket]
            else:
                # Position may already be closed by broker
                pos_check = mt5.positions_get(ticket=ticket)
                if not pos_check:
                    print(f"[FORCE-CLOSE] {self.symbol}: {leg_prefix.upper()} already closed by broker")
                    setattr(self.state, f"{leg_prefix}_ticket", 0)
                    setattr(self.state, f"{leg_prefix}_entry", 0.0)
                    if ticket in self.ticket_map:
                        del self.ticket_map[ticket]
                    if ticket in self.ticket_touch_flags:
                        del self.ticket_touch_flags[ticket]
                else:
                    print(f"[ERROR] {self.symbol}: Failed to force-close {leg_prefix.upper()} (ticket {ticket})")

        await self.save_state()

    async def _check_single_fire_closed(self) -> bool:
        """
        Check if the single fire position was closed (TP or SL).
        If so, nuclear reset all remaining positions and restart.
        Returns True if reset was triggered (caller should return early).
        """
        if self.state.phase != "MONITORING":
            return False
        if not self.state.single_fire_executed:
            return False
        # single_fire_ticket is cleared to 0 by _check_position_drops when it detects the drop
        if self.state.single_fire_ticket != 0:
            return False

        print(f"[SF-CLOSED] {self.symbol}: Single fire closed (TP/SL). Nuclear reset all remaining.")
        self.activity_log.log_info("Recovery trade completed — closing all remaining trades and restarting")
        await self._nuclear_reset_and_restart("SINGLE_FIRE_CLOSED", self.state.realized_pnl)
        return True

    async def _execute_single_fire(self, trigger_price: float, direction: str):
        """Execute the dynamically-determined single fire order."""
        print(f"[SINGLE-FIRE] {self.symbol}: Executing single {direction} "
              f"(lot={self.single_fire_lot}, tp={self.single_fire_tp_pips}, sl={self.single_fire_sl_pips})")
        self.activity_log.log_info(
            f"Placing Recovery {direction.upper()} — lot size: {self.single_fire_lot:.2f}"
        )

        ticket, entry = await self._execute_market_order(
            direction, self.single_fire_lot, "SingleFire",
            tp_pips=self.single_fire_tp_pips, sl_pips=self.single_fire_sl_pips
        )

        if ticket:
            self.state.single_fire_ticket = ticket
            self.state.single_fire_entry = entry
            self.state.single_fire_dir = direction

            if direction == "buy":
                tp = entry + self.single_fire_tp_pips
                sl = entry - self.single_fire_sl_pips
            else:
                tp = entry - self.single_fire_tp_pips
                sl = entry + self.single_fire_sl_pips

            self.activity_log.log_fire(
                self.state.cycle_count, "SingleFire", entry, self.single_fire_lot,
                tp, sl, ticket
            )

        self.state.phase = "MONITORING"
        self.activity_log.log_phase_transition("PAIRS_COMPLETE", "MONITORING")

    # ========================
    # ALL POSITIONS CLOSED CHECK
    # ========================

    async def _check_all_positions_closed(self):
        """
        Check if all tracked positions have been closed.
        If graceful_stop is active: stop completely (do nothing).
        Otherwise: restart the cycle.
        """
        if self.state.phase in ("IDLE", "RESETTING", "AWAITING_SECOND"):
            return

        open_positions = self._get_open_positions_from_state()
        if open_positions:
            return

        # All positions are closed
        if self.graceful_stop:
            print(f"[STOP] {self.symbol}: All positions closed + graceful stop active. Stopping.")
            self.activity_log.log_info("All positions closed with graceful stop active - stopping")
            self.running = False
            self.graceful_stop = False
            self.state.phase = "IDLE"
            self.activity_log.log_stop(self.state.cycle_count, "all_closed_graceful_stop")
            await self.save_state()
        else:
            print(f"[CYCLE-END] {self.symbol}: All positions closed. Restarting cycle.")
            self.activity_log.log_info("All positions closed - restarting cycle")
            await self._nuclear_reset_and_restart("ALL_CLOSED", self.state.realized_pnl)

    # # ========================
    # # LIQUIDATION PRICE SYSTEM (commented out - may be re-implemented later)
    # # ========================
    #
    # def _calculate_liquidation_prices(self):
    #     """
    #     Calculate exact prices where max profit/loss are hit.
    #     """
    #     # Collect all open positions
    #     positions = self._get_open_positions_from_state()
    #
    #     if not positions:
    #         self.state.max_profit_price = float('inf')
    #         self.state.max_loss_price = float('inf')
    #         return
    #
    #     # Calculate net_lots and constant
    #     net_lots = 0.0
    #     constant = 0.0
    #
    #     for direction, entry, lot in positions:
    #         if direction == "buy":
    #             net_lots += lot
    #             constant -= entry * lot
    #         else:  # sell
    #             net_lots -= lot
    #             constant += entry * lot
    #
    #     self.state.net_lots = net_lots
    #     self.state.constant = constant
    #
    #     # Handle fully hedged (net_lots ~ 0)
    #     if abs(net_lots) < 0.0001:
    #         fixed_pnl = constant + self.state.realized_pnl
    #         if fixed_pnl >= self.max_profit_usd:
    #             self.state.max_profit_price = 0  # Immediate trigger
    #         elif fixed_pnl <= -self.max_loss_usd:
    #             self.state.max_loss_price = 0  # Immediate trigger
    #         else:
    #             self.state.max_profit_price = float('inf')
    #             self.state.max_loss_price = float('inf')
    #         return
    #
    #     # Solve for prices
    #     # PnL(P) = P * net_lots + constant + realized_pnl
    #     # For max_profit: max_profit_usd = P * net_lots + constant + realized
    #     self.state.max_profit_price = (
    #         self.max_profit_usd - self.state.realized_pnl - constant
    #     ) / net_lots
    #
    #     self.state.max_loss_price = (
    #         -self.max_loss_usd - self.state.realized_pnl - constant
    #     ) / net_lots
    #
    #     # LOGGING & CONSOLE OUTPUT
    #     print(f"[LIQ-CALC] {self.symbol}:")
    #     print(f"   Inputs: NetLots={net_lots:.2f}, Constant={constant:.2f}, RealizedPnL=${self.state.realized_pnl:.2f}")
    #     print(f"   Config: MaxProfit=${self.max_profit_usd:.2f}, MaxLoss=${self.max_loss_usd:.2f}")
    #     print(f"   TARGETS: MaxProfit Price @ {self.state.max_profit_price:.5f}")
    #     print(f"            MaxLoss Price   @ {self.state.max_loss_price:.5f}")
    #
    #     self.activity_log.log_liquidation_calc(
    #         self.state.max_profit_price,
    #         self.state.max_loss_price,
    #         net_lots,
    #         self.state.realized_pnl
    #     )
    #
    # async def _check_liquidation_prices(self, ask: float, bid: float):
    #     """
    #     Check current Unrealized PnL against max profit/loss thresholds.
    #     Uses mid-price for threshold evaluation.
    #     """
    #     mid = (ask + bid) / 2
    #
    #     # Calculate current PnL: realized + unrealized
    #     # PnL = P * net_lots + constant + realized_pnl
    #     current_pnl = (mid * self.state.net_lots) + self.state.constant + self.state.realized_pnl
    #
    #     if current_pnl >= self.max_profit_usd:
    #         self.activity_log.log_threshold_hit("MAX_PROFIT", mid, current_pnl)
    #         await self._nuclear_reset_and_restart("MAX_PROFIT", current_pnl)
    #         return
    #
    #     if current_pnl <= -self.max_loss_usd:
    #         self.activity_log.log_threshold_hit("MAX_LOSS", mid, current_pnl)
    #         await self._nuclear_reset_and_restart("MAX_LOSS", current_pnl)
    #         return

    # ========================
    # NUCLEAR RESET
    # ========================

    async def _nuclear_reset_and_restart(self, reason: str, total_pnl: float):
        """
        Nuclear reset - close all positions, reset state, then:
        - If graceful_stop is True: stop completely
        - Otherwise: auto-restart new cycle
        """
        old_cycle = self.state.cycle_count

        print(f"[RESET] {self.symbol}: Cycle {old_cycle} ended. Reason: {reason}, PnL: ${total_pnl:.2f}")

        self.state.phase = "RESETTING"
        self.activity_log.log_phase_transition("*", "RESETTING")

        # Close ALL remaining positions for this symbol
        positions = mt5.positions_get(symbol=self.symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                if self._close_position(pos.ticket):
                    closed_count += 1
            print(f"[RESET] {self.symbol}: Closed {closed_count}/{len(positions)} positions")

        # Log reset
        self.activity_log.log_reset(old_cycle, old_cycle + 1, reason, total_pnl)

        # Reset state but increment cycle
        self._reset_state()
        self.state.cycle_count = old_cycle + 1

        # Check if graceful stop was requested - if so, stop completely
        if self.graceful_stop:
            self.running = False
            self.graceful_stop = False
            self.state.phase = "IDLE"
            self.activity_log.log_stop(self.state.cycle_count, "graceful_stop_complete")
            await self.save_state()
            print(f"[STOP] {self.symbol}: Graceful stop complete. Bot fully stopped.")
            return

        # Auto-restart new cycle
        # Must set running=False so start() doesn't exit early on the self.running check
        self.running = False
        print(f"[RESTART] {self.symbol}: Starting new cycle {self.state.cycle_count}")
        await self.start()

    def _reset_state(self):
        """Reset state fields to defaults (except cycle_count)."""
        cycle = self.state.cycle_count
        self.state = StrategyState()
        self.state.cycle_count = cycle
        self.ticket_map.clear()
        self.ticket_touch_flags.clear()

    # ========================
    # HELPERS
    # ========================

    def _get_open_positions_from_state(self) -> list:
        """Return list of (direction, entry, lot) for open positions."""
        positions = []

        if self.state.bx_ticket > 0:
            positions.append(("buy", self.state.bx_entry, self.bx_lot))
        if self.state.sx_ticket > 0:
            positions.append(("sell", self.state.sx_entry, self.sx_lot))
        if self.state.sy_ticket > 0:
            positions.append(("sell", self.state.sy_entry, self.sy_lot))
        if self.state.by_ticket > 0:
            positions.append(("buy", self.state.by_entry, self.by_lot))
        if self.state.single_fire_ticket > 0 and self.state.single_fire_dir:
            positions.append((self.state.single_fire_dir, self.state.single_fire_entry, self.single_fire_lot))

        return positions

    def _clear_ticket_from_state(self, ticket: int):
        """Clear the ticket from state fields."""
        if self.state.bx_ticket == ticket:
            self.state.bx_ticket = 0
            self.state.bx_entry = 0.0
        elif self.state.sx_ticket == ticket:
            self.state.sx_ticket = 0
            self.state.sx_entry = 0.0
        elif self.state.sy_ticket == ticket:
            self.state.sy_ticket = 0
            self.state.sy_entry = 0.0
        elif self.state.by_ticket == ticket:
            self.state.by_ticket = 0
            self.state.by_entry = 0.0
        elif self.state.single_fire_ticket == ticket:
            self.state.single_fire_ticket = 0
            self.state.single_fire_entry = 0.0

    # ========================
    # PERSISTENCE
    # ========================

    async def save_state(self):
        """Persist state to SQLite."""
        # TODO: Implement SQLite persistence
        pass

    async def load_state(self):
        """Load state from SQLite."""
        # TODO: Implement SQLite persistence
        pass

    # ========================
    # STATUS (for API)
    # ========================

    @property
    def current_price(self) -> float:
        """Get current price for the symbol"""
        tick = mt5.symbol_info_tick(self.symbol)
        if tick:
            return (tick.ask + tick.bid) / 2
        return 0.0

    async def start_ticker(self):
        """Called when config updates. Re-sync strategy."""
        pass  # No special handling needed for pair strategy

    def get_status(self) -> dict:
        """Return status dict for API polling."""
        open_count = len(self._get_open_positions_from_state())

        return {
            "running": self.running,
            "phase": self.state.phase,
            "cycle_count": self.state.cycle_count,
            "start_price": self.state.start_price,
            "pairs_complete": self.state.pairs_complete,
            "single_fire_executed": self.state.single_fire_executed,
            "location": self.state.location,
            "single_fire_trigger_price": self.state.single_fire_trigger_price,
            "protection_trigger_price": self.state.protection_trigger_price,
            "open_positions": open_count,
            "realized_pnl": self.state.realized_pnl,
            "graceful_stop": self.graceful_stop,
            "is_resetting": self.state.phase == "RESETTING",
            "step": self.state.cycle_count,
            "iteration": self.state.cycle_count,
            "positions": {
                "bx": {"ticket": self.state.bx_ticket, "entry": self.state.bx_entry},
                "sy": {"ticket": self.state.sy_ticket, "entry": self.state.sy_entry},
                "sx": {"ticket": self.state.sx_ticket, "entry": self.state.sx_entry},
                "by": {"ticket": self.state.by_ticket, "entry": self.state.by_entry},
                "single_fire": {"ticket": self.state.single_fire_ticket, "entry": self.state.single_fire_entry}
            }
        }
