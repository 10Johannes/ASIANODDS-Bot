"""Parser regression tests.

Locks the exact Bet2Invest / OfficialPlay tip format so future parser edits
cannot silently break it (tipster detection, league title, match date, ML bet).

Run: python -m pytest tests/test_parser.py -q
or:  python tests/test_parser.py
"""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from telegram_bot.config import DEFAULT_CONFIG
from telegram_bot.parser import parse_bet_message


# Exact Bet2Invest / OfficialPlay forward format captured from the live channel.
OFFICIALPLAY_ML_TIP = """➡️ 🎯 New tip from OfficialPlay

🎾 Tennis • 📅 Pre-match
🆚 Shuai Zhang vs Kayla Day
🏆 WTA Cincinnati - R1
🕒 Sat, Aug 15, 02:00 PM

🎯 ML Match : Kayla Day @ 1.91 (1 U)"""

# Bet2Invest-style spread export (handicap in sets, date line, min-odds line).
BET2INVEST_SPREAD_TIP = """🎾 Tennis • 📅 Pre-match
🆚 Emerson Jones vs Norov
🏆 ATP Challenger Brownsburg - QF
2025/11/13 20:30
Emerson Jones-1.5 sets
@ 1.505
Cote minimale recommandee: 1.45"""

# French moneyline (PARI … - ML) format handled by _parse_fr_pari_ml_format.
FR_PARI_ML_TIP = """🎾 Tennis • 📅 Pre-match
🆚 Daniil Medvedev vs Alex De Minaur
🏆 Tournoi : ATP Paris
— — 🎯 PARI:  Daniil Medvedev - ML — —
➡️ Prono: @1.3
💰Mise: 1.25u
📈 Min: 1.25"""


def _cfg() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["tipster_settings"] = {}
    cfg["api_sport_ids"] = {}
    return cfg


def _expected_match_date_for_aug15() -> str:
    """Replicate the parser's no-year weekday rollover rule."""
    today = date.today()
    candidate = date(today.year, 8, 15)
    if candidate < today - timedelta(days=60):
        candidate = date(today.year + 1, 8, 15)
    return candidate.isoformat()


class TestBet2InvestOfficialPlayFormat(unittest.TestCase):
    def test_officialplay_ml_tip(self):
        parsed = parse_bet_message(OFFICIALPLAY_ML_TIP, _cfg())
        self.assertIsNotNone(parsed, "OfficialPlay ML tip must parse")
        self.assertEqual(parsed["tipster"], "OfficialPlay")
        self.assertEqual(parsed["sport"], "Tennis")
        self.assertEqual(parsed["sportId"], 3)
        self.assertEqual(parsed["home"], "Shuai Zhang")
        self.assertEqual(parsed["away"], "Kayla Day")
        self.assertEqual(parsed["title"], "WTA Cincinnati - R1")
        self.assertEqual(parsed["market_type"], "ML Match")
        self.assertEqual(parsed["selection"], "Kayla Day")
        self.assertEqual(parsed["selection_type"], "away")
        self.assertIsNone(parsed["side"])
        self.assertIsNone(parsed["handicap"])
        self.assertAlmostEqual(parsed["odds"], 1.91)
        self.assertAlmostEqual(parsed["stake"], 5.0)  # 1 U at base_stake 5
        self.assertEqual(parsed["match_date"], _expected_match_date_for_aug15())

    def test_bet2invest_spread_sets(self):
        parsed = parse_bet_message(BET2INVEST_SPREAD_TIP, _cfg())
        self.assertIsNotNone(parsed, "Bet2Invest spread tip must parse")
        self.assertEqual(parsed["market_type"], "HDP Match")
        self.assertEqual(parsed["selection"], "Emerson Jones")
        self.assertEqual(parsed["handicap"], -1.5)
        self.assertEqual(parsed["preferred_resulting_unit"], "Sets")
        self.assertEqual(parsed["match_date"], "2025-11-13")
        self.assertEqual(parsed["title"], "ATP Challenger Brownsburg - QF")
        self.assertAlmostEqual(parsed["odds"], 1.505)

    def test_french_pari_ml(self):
        parsed = parse_bet_message(FR_PARI_ML_TIP, _cfg())
        self.assertIsNotNone(parsed, "French PARI ML tip must parse")
        self.assertEqual(parsed["market_type"], "ML Match")
        self.assertEqual(parsed["selection"], "Daniil Medvedev")
        self.assertEqual(parsed["selection_type"], "home")
        self.assertEqual(parsed["title"], "ATP Paris")
        self.assertAlmostEqual(parsed["odds"], 1.3)
        self.assertAlmostEqual(parsed["stake"], 6.25)  # 1.25 U at base_stake 5


if __name__ == "__main__":
    unittest.main(verbosity=2)
