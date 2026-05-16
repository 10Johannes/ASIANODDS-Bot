from telegram_bot.api import AsianOddsClient
from dotenv import load_dotenv
import os

load_dotenv()
client = AsianOddsClient(
    os.getenv('ASIANODDS_USERNAME'),
    os.getenv('ASIANODDS_PASSWORD')
)

try:
    running = client.get_running_bets()
    result = running.get('Result', {})
    bets = result.get('RunningBets', [])
    print('Sample running bets (showing first 10):')
    for i, b in enumerate(bets[:10]):
        print(f'  Bet {i+1}: GameId="{b.get("GameId")}", GameType="{b.get("GameType")}", OddsName="{b.get("OddsName")}"')
except Exception as e:
    print(f'Error getting running bets: {e}')
