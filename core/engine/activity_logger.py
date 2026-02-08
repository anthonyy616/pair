"""
Activity Logger for Pair Strategy Engine

Logs all trading activity to downloadable files:
- TP/SL hits, PnL calculations, threshold events, cycle fires, resets
"""

import os
from datetime import datetime
from typing import Optional


class ActivityLogger:
    """
    Per-symbol activity logging with timestamped, downloadable files.
    
    Log files stored in: logs/activity/{user_id}/{symbol}_{date}.log
    """
    
    def __init__(self, symbol: str, user_id: str = "default", session_logger=None):
        self.symbol = symbol
        self.user_id = user_id
        self.session_logger = session_logger
        
        # [FIX] Use absolute path relative to project root to avoid CWD issues
        # core/engine/activity_logger.py -> core/engine -> core -> root -> logs
        from pathlib import Path
        root_dir = Path(__file__).resolve().parent.parent.parent
        self.log_dir = root_dir / "logs" / "users" / user_id / "sessions"
        
        # Ensure directory exists
        os.makedirs(self.log_dir, exist_ok=True)
        
        # Generate filename with date
        date_str = datetime.now().strftime("%Y-%m-%d")
        safe_symbol = symbol.replace(" ", "_")
        # Prefix with 'activity_' so we can distinguish from session logs
        self.log_file = self.log_dir / f"activity_{safe_symbol}_{date_str}.log"
    
    def _write(self, entry: str):
        """Write timestamped entry to log file"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} | {entry}\n"
        
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line)
        
        # Also print to console
        print(f"[{self.symbol}] {entry}")
        
        # [NEW] Also write to session log if available
        if self.session_logger:
            # We use 'log' because it adds its own timestamp
            # Prepend symbol name for context in the merged log
            self.session_logger.log(f"[{self.symbol}] {entry}")
    
    # ========================
    # FIRE EVENTS
    # ========================
    
    def log_fire(self, cycle: int, leg_name: str, price: float, lot: float, 
                 tp: float, sl: float, ticket: int = 0):
        """Log a position opening (atomic fire)"""
        self._write(
            f"[FIRE] C{cycle} {leg_name} OPEN @ {price:.2f} "
            f"(lot={lot:.2f}) TP={tp:.2f} SL={sl:.2f} ticket={ticket}"
        )
    
    def log_second_fire(self, cycle: int, price: float):
        """Log the second atomic fire (grid distance reached)"""
        self._write(f"[FIRE] C{cycle} Grid distance reached @ {price:.2f} → Opening Sx+By")
    
    # ========================
    # TP/SL EVENTS
    # ========================
    
    def log_tp_hit(self, ticket: int, leg: str, tp_price: float, 
                   realized_pnl: float, action: str = ""):
        """Log a take profit hit"""
        self._write(
            f"[TP] {leg} closed @ {tp_price:.2f} | "
            f"pnl=${realized_pnl:+.2f} | {action}"
        )
    
    def log_sl_hit(self, ticket: int, leg: str, sl_price: float, 
                   realized_pnl: float):
        """Log a stop loss hit"""
        self._write(
            f"[SL] {leg} closed @ {sl_price:.2f} | "
            f"pnl=${realized_pnl:+.2f}"
        )
    
    def log_single_buy_opened(self, cycle: int, price: float, lot: float, 
                               tp: float, sl: float, ticket: int = 0):
        """Log recovery single buy opening"""
        self._write(
            f"[SINGLE_BUY] C{cycle} OPEN @ {price:.2f} "
            f"(lot={lot:.2f}) TP={tp:.2f} SL={sl:.2f} ticket={ticket}"
        )
    
    # ========================
    # LIQUIDATION PRICE EVENTS
    # ========================
    
    def log_liquidation_calc(self, profit_price: float, loss_price: float,
                             net_lots: float, realized_pnl: float):
        """Log calculated liquidation prices"""
        self._write(
            f"[LIQUIDATION] profit_price={profit_price:.2f} "
            f"loss_price={loss_price:.2f} | "
            f"net_lots={net_lots:.4f} realized=${realized_pnl:.2f}"
        )
    
    # ========================
    # THRESHOLD EVENTS
    # ========================
    
    def log_threshold_hit(self, threshold_type: str, price: float, 
                          total_pnl: float):
        """Log when max profit/loss/drawdown threshold is hit"""
        self._write(
            f"[THRESHOLD] {threshold_type} hit @ {price:.2f} | "
            f"total_pnl=${total_pnl:+.2f}"
        )
    
    # ========================
    # RESET/LIFECYCLE EVENTS
    # ========================
    
    def log_reset(self, old_cycle: int, new_cycle: int, reason: str, 
                  total_pnl: float):
        """Log nuclear reset and restart"""
        self._write(
            f"[RESET] C{old_cycle}→C{new_cycle} | "
            f"reason={reason} | total_pnl=${total_pnl:+.2f}"
        )
    
    def log_graceful_stop(self, cycle: int, reason: str):
        """Log graceful stop activation"""
        self._write(f"[GRACEFUL_STOP] C{cycle} | reason={reason}")
    
    def log_start(self, cycle: int, start_price: float):
        """Log strategy start"""
        self._write(f"[START] C{cycle} | start_price={start_price:.2f}")
    
    def log_stop(self, cycle: int, reason: str = "manual"):
        """Log strategy stop"""
        self._write(f"[STOP] C{cycle} | reason={reason}")
    
    # ========================
    # DEBUG/INFO
    # ========================
    
    def log_info(self, message: str):
        """Log general info message"""
        self._write(f"[INFO] {message}")
    
    def log_error(self, message: str):
        """Log error message"""
        self._write(f"[ERROR] {message}")
    
    def log_phase_transition(self, old_phase: str, new_phase: str):
        """Log phase state transition"""
        self._write(f"[PHASE] {old_phase} → {new_phase}")
