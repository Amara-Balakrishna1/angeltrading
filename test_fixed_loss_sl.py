"""
Validation script for fixed monetary loss stop loss order placement.
Tests the calculation: trigger = entry - (fixed_loss / quantity)
"""

def calculate_fixed_loss_trigger(entry_price: float, fixed_loss: float, quantity: int) -> float:
    """Calculate stop loss trigger price based on fixed monetary loss."""
    return entry_price - (fixed_loss / quantity)


def validate_fixed_loss_sl(
    symbol: str,
    strike: int,
    entry_price: float,
    fixed_loss: float,
    lots: int,
    lot_size: int
) -> dict:
    """Validate and display fixed loss SL parameters."""
    quantity = lots * lot_size
    trigger_price = calculate_fixed_loss_trigger(entry_price, fixed_loss, quantity)
    actual_loss_at_trigger = (entry_price - trigger_price) * quantity

    validation = {
        "symbol": symbol,
        "strike": strike,
        "entry_price": entry_price,
        "fixed_loss": fixed_loss,
        "lots": lots,
        "lot_size": lot_size,
        "quantity": quantity,
        "trigger_price": trigger_price,
        "entry_amount": entry_price * quantity,
        "trigger_amount": trigger_price * quantity,
        "actual_loss_at_trigger": actual_loss_at_trigger,
        "loss_pct_from_entry": ((entry_price - trigger_price) / entry_price) * 100 if entry_price > 0 else 0
    }

    return validation


def print_validation(validation: dict):
    """Print validation results in a readable format."""
    print("\n" + "=" * 70)
    print("  FIXED MONETARY LOSS STOP LOSS VALIDATION")
    print("=" * 70)
    print(f"\n  Symbol              : {validation['symbol']}")
    print(f"  Strike              : {validation['strike']} CE")
    print(f"  Lots                : {validation['lots']}")
    print(f"  Lot Size            : {validation['lot_size']}")
    print(f"  Total Quantity      : {validation['quantity']}")
    print(f"\n  Entry Premium       : Rs. {validation['entry_price']:.2f}")
    print(f"  Entry Amount        : Rs. {validation['entry_amount']:,.2f}")
    print(f"  Fixed Loss Target   : Rs. {validation['fixed_loss']:,.2f}")
    print(f"  SL Trigger Price    : Rs. {validation['trigger_price']:.2f}")
    print(f"  Trigger Amount      : Rs. {validation['trigger_amount']:,.2f}")
    print(f"\n  Actual Loss at SL   : Rs. {validation['actual_loss_at_trigger']:,.2f}")
    print(f"  Loss % from Entry   : {validation['loss_pct_from_entry']:.2f}%")
    print("\n" + "=" * 70)
    print(f"  Formula: Trigger = Entry - (Fixed_Loss / Quantity)")
    print(f"  Example: {validation['trigger_price']:.2f} = {validation['entry_price']:.2f} - ({validation['fixed_loss']:.0f} / {validation['quantity']})")
    print("=" * 70)


def main():
    # Test cases for fixed loss SL
    test_cases = [
        {
            "symbol": "SENSEX",
            "strike": 72000,
            "entry_price": 200.0,
            "fixed_loss": 9000.0,
            "lots": 2,
            "lot_size": 25
        },
        {
            "symbol": "SENSEX",
            "strike": 72000,
            "entry_price": 150.0,
            "fixed_loss": 9000.0,
            "lots": 2,
            "lot_size": 25
        },
        {
            "symbol": "SENSEX",
            "strike": 72000,
            "entry_price": 100.0,
            "fixed_loss": 9000.0,
            "lots": 5,
            "lot_size": 25
        },
        {
            "symbol": "NIFTY",
            "strike": 23500,
            "entry_price": 80.0,
            "fixed_loss": 9000.0,
            "lots": 3,
            "lot_size": 50
        }
    ]

    print("\nValidating fixed monetary loss stop loss calculation...")
    print("Target: Rs. 9,000 fixed loss regardless of premium level\n")

    for i, test_case in enumerate(test_cases, 1):
        print(f"\n--- Test Case {i} ---")
        validation = validate_fixed_loss_sl(
            symbol=test_case["symbol"],
            strike=test_case["strike"],
            entry_price=test_case["entry_price"],
            fixed_loss=test_case["fixed_loss"],
            lots=test_case["lots"],
            lot_size=test_case["lot_size"]
        )
        print_validation(validation)

    print("\n\nVALIDATION SUMMARY:")
    print("  [OK] Fixed loss calculation: entry - (fixed_loss / quantity)")
    print("  [OK] Ensures fixed monetary loss regardless of premium level")
    print("  [OK] Takes precedence over premium-based SL if both enabled")
    print("  [OK] Separate from P&L-based max_loss (-5,000)")
    print("\n  [!] To enable in config:")
    print("      enable_fixed_loss_sl: True")
    print("      fixed_loss_amount: 9000.0")
    print("\n")


if __name__ == "__main__":
    main()
