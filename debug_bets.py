from telegram_bot.api import AsianOddsClient
from dotenv import load_dotenv
import os

load_dotenv()
client = AsianOddsClient(
    os.getenv('ASIANODDS_USERNAME'),
    os.getenv('ASIANODDS_PASSWORD')
)
client.login()
client.register()

try:
    running = client.get_running_bets()
    bets = client.parse_running_bets(running)
    print(f'Running bets count: {len(bets)}')
    print('Sample running bets (showing first 10):')
    for i, b in enumerate(bets[:10]):
        print(f'  Bet {i+1}: HomeName="{b.get("HomeName")}", AwayName="{b.get("AwayName")}", BetType="{b.get("BetType")}", Odds={b.get("Odds")}, Stake={b.get("Stake")}, Bookie="{b.get("Bookie")}", Ref="{b.get("ReferenceNumber")}"')
except Exception as e:
    print(f'Error getting running bets: {e}')
