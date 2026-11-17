#!/usr/bin/env python3
"""
Test Hyperliquid API connection and fetch wallet balances.

Loads credentials from .env and prints both Spot and Perp wallet balances.
"""

import os
import sys
from pathlib import Path

# Load .env file
from dotenv import load_dotenv

# Add src to path for any local imports if needed
sys.path.insert(0, str(Path(__file__).parent / "src"))

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from eth_account import Account


def main():
    # Load environment variables from .env
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(f"ERROR: .env file not found at {env_path}")
        sys.exit(1)

    load_dotenv(env_path)

    # Get credentials
    account_address = os.getenv("HL_ACCOUNT_ADDRESS")
    api_secret = os.getenv("HL_API_SECRET")
    env = os.getenv("HL_ENV", "testnet").lower()

    if not account_address or not api_secret:
        print("ERROR: HL_ACCOUNT_ADDRESS and HL_API_SECRET must be set in .env")
        sys.exit(1)

    # Determine base URL
    urls = {"testnet": "https://api.hyperliquid-testnet.xyz", "mainnet": "https://api.hyperliquid.xyz"}
    base_url = urls.get(env)
    if not base_url:
        print(f"ERROR: Invalid HL_ENV: {env}. Must be 'testnet' or 'mainnet'")
        sys.exit(1)

    print(f"Environment: {env}")
    print(f"Base URL: {base_url}")
    print(f"Account Address: {account_address}")
    print("-" * 60)

    # Initialize Info client (read-only, no wallet needed)
    info = Info(base_url, skip_ws=True)

    # Initialize Exchange client (for authenticated calls if needed)
    wallet = Account.from_key(api_secret)
    # API wallet acts on behalf of master account
    acct = None if account_address.lower() == wallet.address.lower() else account_address
    exchange = Exchange(wallet, base_url, account_address=acct)

    try:
        # Fetch Perp Wallet (Clearinghouse State)
        print("\n=== PERP WALLET (Clearinghouse State) ===")
        perp_state = info.user_state(account_address)
        print(f"Account Value: ${float(perp_state['marginSummary']['accountValue']):,.2f}")
        print(f"Total Margin Used: ${float(perp_state['marginSummary']['totalMarginUsed']):,.2f}")
        print(f"Total NTL Pos: ${float(perp_state['marginSummary']['totalNtlPos']):,.2f}")
        print(f"Total Raw USD: ${float(perp_state['marginSummary']['totalRawUsd']):,.2f}")

        # Print positions
        print("\nPositions:")
        for ap in perp_state.get("assetPositions", []):
            pos = ap["position"]
            coin = pos["coin"]
            szi = float(pos["szi"])
            entry_px = float(pos.get("entryPx", 0) or 0)
            unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
            if szi != 0:
                print(f"  {coin}: size={szi:.6f}, entry_px=${entry_px:,.2f}, unrealized_pnl=${unrealized_pnl:,.2f}")

        # Fetch Spot Wallet (Spot State)
        print("\n=== SPOT WALLET (Spot State) ===")
        spot_state = info.spot_user_state(account_address)
        balances = spot_state.get("balances", [])

        if not balances:
            print("No spot balances found.")
        else:
            total_usd = 0.0
            for b in balances:
                coin = b.get("coin", "UNKNOWN")
                total = float(b.get("total", 0) or 0)
                hold = float(b.get("hold", 0) or 0)
                available = total - hold
                if total > 0:
                    print(f"  {coin}: total={total:.6f}, hold={hold:.6f}, available={available:.6f}")
                    # Note: We don't have USD prices here, just raw amounts

        # Also fetch meta for reference
        print("\n=== EXCHANGE META ===")
        meta = info.meta()
        print(f"Universe: {len(meta['universe'])} perps")
        spot_meta = info.spot_meta()
        print(f"Spot tokens: {len(spot_meta['universe'])}")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()