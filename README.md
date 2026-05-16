# AsianOdds Telegram Betting Bot

An automated Telegram bot that monitors betting tip channels and places bets on AsianOdds based on received tip messages. The bot supports multiple sports, tipster management, and configurable betting strategies.

## Features

- 🤖 **Automated Bet Placement**: Automatically places bets on AsianOdds based on tip messages from Telegram channels
- 🎾 **Multi-Sport Support**: Supports Tennis, Football/Soccer, and Basketball
- 📊 **Tipster Management**: Configure individual settings for different tipsters
- ⚙️ **Flexible Configuration**: Customizable stake limits, odds tolerance, and betting rules
- 🔄 **Automatic Retry**: Configurable retry mechanisms for event resolution and bet placement
- 📈 **Odds Validation**: Automatic odds checking with minimum odds requirements and tolerance
- 🎯 **Bet Types**: Supports Moneyline (1X2), Handicap (HDP), and Over/Under (OU) bets
- 📱 **Telegram Integration**: Listens to tip channels and forwards bet information
- 🐳 **Docker Support**: Easy deployment with Docker and docker-compose
- 🏦 **Multi-Bookie Support**: Access multiple bookmakers through AsianOdds

## Prerequisites

- Python 3.12 or higher
- AsianOdds account with API access
- Telegram API credentials (API_ID and API_HASH)
- Access to a Telegram channel with betting tips

## Installation

### 1. Clone the Repository

```bash
git clone <repository-url>
cd "PS3838-Bot"
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment Configuration

Create a `.env` file in the project root with the following variables:

```env
# Telegram API Credentials
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash

# Telegram Channel Configuration
TELEGRAM_CHANNEL=@your_channel  # Main channel for listening and logging
FORWARDER_CHANNELS=@forwarder_channel1,@forwarder_channel2  # Optional: Channel(s) to forward bet info
LISTENER_CHANNELS=@additional_listener  # Optional: Additional channel(s) to listen to

# AsianOdds API Credentials
ASIANODDS_USERNAME=your_asianodds_username
ASIANODDS_PASSWORD=your_asianodds_password
```

### 4. Initial Configuration

The bot uses `config.json` for betting configuration. A default configuration is created automatically, but you can customize it:

```json
{
    "base_stake": 5,
    "min_stake": 5,
    "max_stake": 20,
    "min_unit": 0.5,
    "max_unit": 2,
    "channel_settings": {
        "@somechannel": { "base_stake": 10, "max_stake": 50 },
        "Some Channel Title": { "base_stake": 7.5, "min_unit": 1, "max_unit": 3 },
        "-1001234567890": { "base_stake": 6 }
    },
    "odds_tolerance": 0.05,
    "allow_tennis": true,
    "allow_soccer": true,
    "allow_football": true,
    "allow_basketball": false
}
```

## Usage

### Running the Bot

```bash
python main.py
```

On first run, you'll be prompted to authenticate with Telegram. Enter the code sent to your Telegram account.

### Docker Deployment

#### Using Docker Compose (Recommended)

```bash
docker-compose up -d
```

#### Using Docker Directly

```bash
docker build -t asianodds-bot .
docker run -d --env-file .env --name asianodds-bot asianodds-bot
```

## Bot Commands

The bot responds to commands sent in the configured Telegram channel:

### Basic Commands

- `/help` - Show help message with all available commands
- `/showconfig` - Display current configuration
- `/balance` - Show current AsianOdds account balance
- `/exportwagers [days]` - Send an export of wager history (defaults to the last 7 days, max 30)
- `/restart` - Restart the bot

### Stake Management

- `/stake <value>` - Set base stake (minimum €5)
- `/minstake <value>` - Set minimum allowed stake
- `/maxstake <value>` - Set maximum allowed stake
- `/minunit <value>` - Set minimum unit size (e.g., 0.5)
- `/maxunit <value>` - Set maximum unit size (e.g., 5)

### Sports Configuration

- `/sports <tennis|football|basketball|both|all>` - Enable/disable betting on specific sports
  - `tennis` - Enable only Tennis
  - `football` - Enable only Football
  - `basketball` - Enable only Basketball
  - `both` - Enable Tennis and Football
  - `all` - Enable all sports

### Odds Configuration

- `/odds <tolerance>` - Set odds tolerance (e.g., `/odds 0.05`)
  - Allows bets when API odds are within tolerance of tip odds
- `/channelodds <channel> <tolerance>` - Set per-channel odds tolerance override
- `/channeloddslist` - List per-channel odds tolerance overrides
- `/channeloddsremove <channel>` - Remove a per-channel odds tolerance override
- `/tipsterodds <tipster> <tolerance>` - Set tipster-specific odds tolerance override
- `/tipsteroddslist` - List tipster-specific odds tolerance overrides
- `/tipsteroddsremove <tipster>` - Remove a tipster odds tolerance override

### Bet Type Configuration

- `/bettype <prematch|live|both>` - Configure bet type preference
  - `prematch` - Accept only pre-match bets (Today/Early markets)
  - `live` - Accept only live bets (Live market)
  - `both` - Accept both pre-match and live bets (default)

### Retry Configuration

- `/retry <attempts> <minutes>` - Configure event resolution retry
- `/betretry <attempts> <minutes>` - Configure bet placement retry

### Tipster Management

- `/tipsterstake <tipster> <base|min|max|minunit|maxunit> <value>` - Set tipster-specific settings
- `/tipsterlist` - List all configured tipsters with their settings
- `/tipsterremove <tipster>` - Remove tipster-specific settings

### Channel Stake Overrides

- `/channelstake <channel> <base|min|max|minunit|maxunit> <value>` - Set per-channel stake settings
- `/channelstakelist` - List all configured channel stake overrides
- `/channelstakeremove <channel>` - Remove a channel stake override

### Channel Management

- `/setchannel [channel|none|blank|clear]` - Set main channel (TELEGRAM_CHANNEL)
- `/setforwarder [channel(s)|none|blank|clear]` - Set forwarder channel(s)
- `/setlistener [channel(s)|none|blank|clear]` - Set additional listener channel(s)
- `/showchannels` - Show current channel configuration

## AsianOdds API Overview

### Authentication Flow

1. **Login**: Authenticate with username and MD5-hashed password
2. **Register**: Complete authorization within 60 seconds of login
3. **Use AOToken**: All subsequent requests use the AOToken header

### Market Types

- `0` - Live (in-play)
- `1` - Today (matches starting today)
- `2` - Early (future matches)

### Game Types

- `H` - Handicap (Asian Handicap)
- `O` - Over/Under (Totals)
- `X` - 1X2 (Moneyline/Match Winner)

### Odds Formats

- `00` - Decimal/European (default)
- `MY` - Malaysian
- `HK` - Hong Kong

### Session Management

- Sessions timeout after 5 minutes of inactivity
- The bot automatically re-authenticates when needed

## Project Structure

```
PS3838-Bot/
├── main.py                    # Entry point
├── requirements.txt           # Python dependencies
├── config.json               # Bot configuration
├── .env                      # Environment variables (create this)
├── .env.example              # Example environment file
├── Dockerfile               # Docker configuration
├── docker-compose.yml        # Docker Compose configuration
├── telegram_bot/
│   ├── __init__.py
│   ├── telegram_bot.py      # Main bot logic
│   ├── api.py               # AsianOdds API client
│   ├── betting.py           # Bet placement logic
│   ├── parser.py            # Message parsing
│   ├── resolver.py          # Event/line resolution
│   ├── validation.py        # Odds validation
│   ├── config.py            # Configuration management
│   ├── env.py               # Environment variable loading
│   ├── logger.py            # Logging utilities
│   └── state.py             # State management
└── debug_bets.py            # Debug utility for checking bets
```

## Configuration Details

### Config.json Options

- `base_stake`: Default stake amount
- `min_stake`: Minimum allowed stake
- `max_stake`: Maximum allowed stake
- `min_unit`: Minimum unit multiplier
- `max_unit`: Maximum unit multiplier
- `odds_tolerance`: Allowed difference between tip odds and API odds
- `allow_tennis`: Enable/disable Tennis betting
- `allow_soccer`: Enable/disable Soccer/Football betting
- `allow_football`: (legacy) mirrors `allow_soccer` for backwards compatibility
- `allow_basketball`: Enable/disable Basketball betting
- `allow_prematch`: Enable/disable pre-match bets (default: true)
- `allow_live`: Enable/disable live bets (default: true)
- `retry_attempts`: Number of retry attempts for event resolution
- `retry_interval_minutes`: Minutes between retries
- `place_result_retry_attempts`: Number of retry attempts for bet placement
- `place_result_retry_interval_minutes`: Minutes between bet placement retries
- `tipster_settings`: Tipster-specific configurations
- `channel_settings`: Per-channel overrides for stake settings

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `API_ID` | Telegram API ID | Yes |
| `API_HASH` | Telegram API Hash | Yes |
| `TELEGRAM_CHANNEL` | Main channel for listening and logging | Yes |
| `FORWARDER_CHANNELS` | Channel(s) to forward bet information | No |
| `LISTENER_CHANNELS` | Additional channel(s) to listen to | No |
| `ASIANODDS_USERNAME` | AsianOdds account username | Yes |
| `ASIANODDS_PASSWORD` | AsianOdds account password | Yes |

## How It Works

1. **Message Listening**: Bot listens to configured Telegram channels for betting tips
2. **Message Parsing**: Parses tip messages to extract:
   - Sport type
   - Players/Teams
   - Bet type (ML, HDP, Total Points)
   - Handicap (if applicable)
   - Odds
   - Stake units
   - Minimum odds requirement
3. **Event Resolution**: Finds matching event in AsianOdds fixtures using GetMatches
4. **Odds Retrieval**: Gets current odds from AsianOdds using GetFeeds
5. **Placement Info**: Retrieves min/max stake limits using GetPlacementInfo
6. **Odds Validation**: Checks if API odds meet requirements
7. **Bet Placement**: Places bet using PlaceBet if all validations pass
8. **Retry Logic**: Automatically retries if event resolution or bet placement fails

## Troubleshooting

### Bot Not Responding to Commands

- Ensure the bot is running and authenticated with Telegram
- Check that `TELEGRAM_CHANNEL` is set correctly
- Verify the bot has access to the channel

### Bets Not Being Placed

- Check AsianOdds account balance and credit
- Verify API credentials are correct
- Check odds tolerance settings
- Review logs for error messages
- Ensure the event is found in AsianOdds fixtures

### Authentication Issues

- Delete `session_asianodds.session` and restart the bot
- Ensure `API_ID` and `API_HASH` are correct
- Complete Telegram authentication when prompted
- For AsianOdds: verify username and password are correct

### Event Not Found

- The event might not be available in AsianOdds yet
- Check league name matches
- Verify player/team names match
- Check retry configuration to allow more time for event to appear

### Odds Too Low

- The bot checks for minimum odds requirements
- Adjust `odds_tolerance` if needed
- Check if tip message includes "No bet under X" requirement

## Debug Files

The bot creates debug files for troubleshooting:

- `debug_feeds_test.json`: Last feeds/odds response from AsianOdds
- `debug_matches_test.json`: Last matches/fixtures data retrieved

These files can help diagnose issues with event resolution and odds retrieval.

## Security Notes

- Never commit `.env` file to version control
- Keep API credentials secure
- Use environment variables for sensitive data
- Regularly rotate API credentials
- AsianOdds passwords are MD5-hashed before transmission

## License

[Add your license information here]

## Support

For issues, questions, or contributions, please [create an issue](link-to-issues) or contact the maintainers.

## Changelog

### Recent Updates

- Migrated from PS3838 (Pinnacle) API to AsianOdds API
- Updated authentication flow for AsianOdds (Login → Register → AOToken)
- Added support for AsianOdds market types (Live, Today, Early)
- Added support for AsianOdds game types (Handicap, OverUnder, 1X2)
- Updated bet placement to use AsianOdds PlaceBet endpoint
- Added GetPlacementInfo call before bet placement for stake limits
