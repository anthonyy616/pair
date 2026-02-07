# Groups + 3-CAP TP-Driven Trading System

## Overview

This document describes the TP-driven multi-group trading system implemented in `symbol_engine.py`. The system manages grid trading with atomic pair execution, dynamic grid expansion, and group rollover via **artificial TP** when C reaches 3.

---

## Core Concepts

### Pairs

A **pair** consists of a BUY and SELL position at related price levels:

- **Pair N (positive)**: Buy at `anchor + N*D`, Sell at `anchor + (N-1)*D`
- **Pair 0**: Buy at `anchor`, Sell at `anchor - D`
- **Pair N (negative)**: Buy at `anchor + N*D`, Sell at `anchor + (N-1)*D`

Where `D` = spread (grid distance) and `anchor` = initial price.

### Pair States

- **Incomplete**: Only one leg filled (BUY xor SELL)
- **Complete**: Both legs filled (BUY and SELL) - **EVER FILLED, regardless of current MT5 state**

### Completed Pairs Count (C)

`C` = number of pairs with BOTH positions currently open in MT5 for a **specific group**.

- Counted via MT5 positions + ticket_map (authoritative)
- Group-scoped: `_count_completed_pairs_for_group(group_id)`

### Groups

Groups are isolated namespaces for pairs:

- **Group 0**: Pairs 0-99 (and negative pairs -1 to -99)
- **Group 1**: Pairs 100-199 (and -100 to -199)
- **Group 2**: Pairs 200-299, etc.

Each group maintains its own anchor price and pair indices.

---

## Trading Flow

### Phase 1: INIT

When bot starts or new group begins:

1. Place **B(offset)** (Buy at anchor) - Pair offset is incomplete
2. Place **S(offset+1)** (Sell at anchor) - Pair offset+1 is incomplete
3. Both use **lot index 0** (smallest lot size)

**State after INIT:**

- Pair offset: B only (incomplete)
- Pair offset+1: S only (incomplete)
- C = 0

### Phase 2: Dynamic Grid Expansion (Atomic) - ALL GROUPS

**IMPORTANT: All groups expand normally via step triggers, not just Group 0.**

Grid expands atomically as price moves, until C reaches 2:

**Price goes UP (bullish):**

1. At trigger level: Place **B(n) + S(n+1)** atomically
   - B(n) completes pair n
   - S(n+1) starts new incomplete pair
   - C increases by 1

**Price goes DOWN (bearish):**

1. At trigger level: Place **S(n) + B(n-1)** atomically
   - S(n) completes pair n
   - B(n-1) starts new incomplete pair
   - C increases by 1

### Phase 3: Non-Atomic Expansion (C == 2)

When C == 2 and expansion triggers:

- **Only place the completing leg** (no seed leg)
- This makes C = 3
- **Immediately call `_force_artificial_tp_and_init()`**:
  1. Find and close the remaining incomplete pair at the OPPOSITE edge
  2. Fire INIT for next group at the event price

### Phase 4: Toggle Trading (C == 3)

When C = 3:

- **No new pair legs created** (grid stops expanding)
- **Toggle trading continues** on completed pairs:
  - Each completed pair can trade: buy → sell → buy → sell...
  - Until max_positions reached → hedge executes
- Group is "locked" but still active for toggle trading
- **Expansion Lock**: Once Group N+1 is initialized, Group N is **permanently locked for expansion**. Only toggle triggers (re-entries/hedges) are permitted.

### Phase 5: TP-Driven Events

**Incomplete Pair TP → Fire INIT for Next Group:**

- When an incomplete pair's leg hits TP, INIT fires for the next group
- **Permanent Lock Logic**: `_incomplete_pairs_init_triggered` set prevents duplicate INITs from the same pair
- This allows infinite group progression without waiting for C=3 non-atomic

**Completed Pair TP → Conditional Expansion:**

- When a completed pair's leg hits TP (in current or prior group)
- **SKIP if normal expansion is active (C < 3)**: This prevents double expansion from both step triggers and TP-driven expansion
- **Permanent Lock Logic**: Once a pair fires expansion, it is permanently added to `_pairs_tp_expanded` and cannot fire expansion again
- If C == 3, triggers expansion via artificial TP mechanism

---

### Phase 6: Directional Guards (Global)

- **Initial Bias**: Captured during Group 1 INIT (`self.group_direction`).
- **Global Restriction**: Blocks further expansion in the direction of the initial trend for **ALL** groups.
- **Goal**: Prevent the bot from "chasing" the trend indefinitely via step triggers.

---

## Artificial TP Concept (Critical for Infinite Groups)

### Why Artificial TP?

When C reaches 3 via non-atomic expansion:

1. The grid stops expanding (locked)
2. One incomplete pair remains at the edge
3. Instead of waiting for this pair to naturally hit TP (which could take forever or hit on wrong edge), we **immediately**:
   - Close the incomplete pair position
   - Fire INIT for the next group

### Flow

```
C==2 → Expansion triggers → Non-atomic completes (C=3)
    ↓
_force_artificial_tp_and_init() called immediately
    ↓
1. Find incomplete pair in current group (via MT5 + ticket_map)
2. Close that position (_close_position)
3. Fire _execute_group_init(current_group + 1, event_price)
    ↓
New group starts with B(new_offset) + S(new_offset+1)
```

---

## Example: Group 0 to Group 1 (Bullish Scenario)

### Group 0

```
INIT at anchor=100.00, D=30:
  B0 @ 100.00 (Pair 0 incomplete)
  S1 @ 100.00 (Pair 1 incomplete)
  C = 0

Price rises to 130:
  B1 + S2 → Pair 1 complete, Pair 2 incomplete
  C = 1

Price rises to 160:
  B2 + S3 → Pair 2 complete, Pair 3 incomplete
  C = 2

Price rises to 190:
  NON-ATOMIC: B3 only (C becomes 3)
  → Immediately: _force_artificial_tp_and_init()
  → Close S3 (incomplete pair)
  → Fire INIT for Group 1 at 190.00
```

### Group 1 INIT

```
Group 1 INIT at anchor=190.00:
  B100 @ 190.00 (Pair 100 incomplete)
  S101 @ 190.00 (Pair 101 incomplete)
  C_g1 = 0

Grid continues expanding in Group 1...
```

---

## Multiple Groups Can Be Open Simultaneously

**Question:** With spread=30 and TP=90, can more than 2 groups be open?

**Answer:** YES. Multiple groups can have open positions simultaneously.

### Why?

1. When Group 0 locks at C=3, it has 3 completed pairs still toggle-trading
2. Group 1 starts while Group 0 positions are still open
3. When Group 1 locks at C=3, Group 2 starts
4. At this point: Group 0, Group 1, and Group 2 all have positions

### Example Timeline

```
Time T1: Group 0 active, C=3 (locked), 6 positions open (3 pairs × 2 legs)
Time T2: Group 1 INIT fires, adds 2 more positions (B100, S101)
         Total: 8 positions (Group 0: 6, Group 1: 2)
Time T3: Group 1 expands, adds more positions
         Group 0 positions may close via TP/SL
Time T4: Group 1 locks at C=3, Group 2 INIT fires
         Could have positions from Group 0, 1, and 2 simultaneously
```

### What Drives This?

- TP distance (90) vs spread (30) = 3x ratio
- Price volatility and direction changes
- Toggle trading on locked groups keeps positions alive
- Each group's completed pairs continue trading independently

---

## Key Implementation Details

### Ticket Map (Canonical 5-Tuple)

```python
ticket_map[position_ticket] = (pair_idx, leg, entry_price, tp_price, sl_price)
```

- `pair_idx`: The pair index (0, 1, 100, 101, etc.)
- `leg`: 'B' or 'S'
- `entry_price`: Actual execution price
- `tp_price`: Take profit level
- `sl_price`: Stop loss level

### Locked Entry Prices (Grid Drift Prevention)

```python
pair.locked_buy_entry = exec_price   # Set ONCE on first BUY execution
pair.locked_sell_entry = exec_price  # Set ONCE on first SELL execution
```

- Set in `_execute_market_order()` when trade first executes
- Used in `_check_virtual_triggers()` for re-entry triggers
- **NEVER CHANGED** after first execution (immutable)
- Ensures re-entries happen at exact same price level

### TP/SL Touch Flags (Deterministic Detection)

```python
ticket_touch_flags[ticket] = {'tp_touched': False, 'sl_touched': False}
```

- Latched on every tick by `_update_tp_sl_touch_flags()`
- BUY: `tp_touched` when `bid >= tp_price`, `sl_touched` when `bid <= sl_price`
- SELL: `tp_touched` when `ask <= tp_price`, `sl_touched` when `ask >= sl_price`
- Used in `_check_position_drops()` to classify closed positions

### Completed/Incomplete Determination

**Uses in-memory pair state, NOT MT5 positions:**

```python
pair = self.pairs.get(pair_idx)
was_completed = pair and pair.buy_filled and pair.sell_filled
```

- `pair.buy_filled` and `pair.sell_filled` remember "ever filled"
- Even if one leg closed via SL, pair is still considered "completed"
- This allows proper TP routing for the survivor leg

### C Counting (MT5-Authoritative)

```python
def _count_completed_pairs_for_group(self, group_id: int) -> int:
    positions = mt5.positions_get(symbol=self.symbol)
    # Build pair_idx -> set of legs from ticket_map
    # Count pairs where legs == {'B', 'S'}
    # Filter by _get_group_from_pair(pair_idx) == group_id
```

- Counts what's ACTUALLY OPEN in MT5
- Group-scoped for proper gating

---

## Key Files and Methods

| Method | Purpose |
|--------|---------|
| `_execute_group_init()` | Create B(offset) + S(offset+1) for new group |
| `_expand_bullish/bearish()` | Atomic expansion, calls artificial TP at C==2 |
| `_force_artificial_tp_and_init()` | Close incomplete pair + fire INIT for next group |
| `_check_position_drops()` | Detect closed positions, route TP/SL events |
| `_update_tp_sl_touch_flags()` | Latch TP/SL crossings on every tick |
| `_count_completed_pairs_for_group()` | MT5-authoritative C counting |
| `_execute_tp_expansion()` | TP-driven expansion with artificial TP at C==2 |

---

## Configuration

| Parameter | Description |
|-----------|-------------|
| `spread` | Grid distance (D) between levels |
| `tp_pips` | Take profit distance from entry |
| `sl_pips` | Stop loss distance from entry |
| `max_positions` | Max trades per pair before hedge |
| `lot_sizes` | Array of lot sizes by trade index |
| `GROUP_OFFSET` | Pair index offset per group (default: 100) |

---

## Trade Audit Logging

For easier debugging and auditability, the system maintains a `logs/trade_audit_{symbol}.csv` file.

### Column Structure

1. **#No**: Sequential trade number (persistent).
2. **Pair**: Pair index (e.g., 0, 1, 100, 101).
3. **Type**: BUY or SELL.
4. **Name**: Leg name (e.g., B0, S1, S100).
5. **Entry**: Actual execution price.
6. **TP**: Take Profit level at entry.
7. **SL**: Stop Loss level at entry.
8. **Trade No**: `trade_count` (how many times this pair has cycled).
9. **Activity**: Descriptive log of the event (e.g., "ENTRY", "TP HIT", "SL HIT", "ARTIFICIAL TP & INIT").
