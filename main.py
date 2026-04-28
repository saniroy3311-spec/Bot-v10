import asyncio
import logging
from dotenv import load_dotenv

from feed.ws_feed import CandleFeed
from indicators.engine import compute
from strategy.signal import evaluate, SignalType
from execution import ExecutionEngine

# Set up basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("shiva_sniper_main")

load_dotenv()

async def main():
    logger.info("Starting Shiva Sniper Bot - Local WebSocket Engine (No Webhook)...")
    
    # Initialize the execution engine wrapper (which handles OrderManager and TrailMonitor)
    engine = ExecutionEngine()
    await engine.initialize()
    
    # Initialize the local Delta Exchange WebSocket feed
    feed = CandleFeed()
    await feed.connect()

    try:
        # Stream live candle data continuously
        async for df in feed.stream():
            try:
                # 1. Compute indicators
                snap = compute(df)
                
                # 2. Evaluate signals locally
                sig = evaluate(snap, False)
                
                # 3. Pass the closed bar and signal to the execution engine
                # This triggers entries and bar-close trail updates automatically
                await engine.process_closed_bar(snap, sig)
                
            except Exception as e:
                logger.error(f"Calculation/Execution Error: {e}")
                
    except asyncio.CancelledError:
        logger.info("Bot execution cancelled.")
    finally:
        await feed.disconnect()
        await engine.shutdown()
        logger.info("Bot shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting...")
