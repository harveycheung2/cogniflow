import sys
import time
from pathlib import Path

# Ensure agentic_os_v2 is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from services.telegram_bot import telegram_bot_service

def main():
    print("=" * 60)
    print("  Starting CogniFlow OS Telegram Bot Service")
    print("=" * 60)

    bot_info = telegram_bot_service.get_me()
    if not bot_info:
        print("Error: Failed to authenticate with Telegram. Verify TELEGRAM_BOT_TOKEN in .env.")
        sys.exit(1)

    print(f"Connected as: @{bot_info.get('username')} ({bot_info.get('first_name')})")
    print("Listening for incoming Telegram messages...")
    print("Press Ctrl+C to stop.")

    try:
        telegram_bot_service.poll_loop()
    except KeyboardInterrupt:
        print("Stopping Telegram bot gracefully...")
        telegram_bot_service.stop_polling()
        print("Stopped.")

if __name__ == "__main__":
    main()
