"""Crypto precision, historical fee costs, and mixed-asset recovery checks."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from arena.autonomous import AutonomousResearch
from arena.core import Engine, EngineError
from arena.resolution import _inventory_matches, _orders_match


CRYPTO = {"symbol": "BTC/USD", "class": "crypto", "exchange": "ALPACA",
          "name": "Bitcoin USD", "status": "active", "tradable": True, "fractionable": True}


class CryptoLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(Path(self.tmp.name) / "arena.sqlite3")
        self.engine.configure({"mode": "paper", "asset_scope": "equities_crypto", "total_capital": 250,
                               "loss_limit": 100, "position_cap_pct": 50, "exposure_cap_pct": 50, "fee_bps": 1000,
                               "agents": [{"id": "openai", "name": "Test", "provider": "openai"}]})
        self.engine.register_asset("BTC/USD", CRYPTO)
        self.engine.resume()

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def agent(self):
        return self.engine.snapshot()["agents"][0]

    def buy(self):
        order = self.engine.reserve_order("openai", "BTC/USD", "buy", 100, notional=25)
        self.engine.update_order(order["id"], {"id": "crypto-buy", "status": "filled",
                                              "filled_qty": ".25", "filled_avg_price": "100"})
        return order

    def test_nine_decimal_partial_fill_is_recorded_and_repeated_fill_is_idempotent(self):
        order = self.engine.reserve_order("openai", "BTC/USD", "buy", 100, notional=25)
        partial = {"id": "crypto-buy", "status": "partially_filled", "filled_qty": "0.000000001",
                   "filled_avg_price": "100"}
        self.engine.update_order(order["id"], partial)
        self.assertAlmostEqual(self.agent()["positions"]["BTC/USD"]["qty"], 1e-9, places=12)
        cash = self.agent()["cash"]
        self.engine.update_order(order["id"], partial)
        self.assertEqual(self.agent()["cash"], cash)
        self.engine.update_order(order["id"], {**partial, "status": "filled", "filled_qty": ".25"})
        self.assertAlmostEqual(self.agent()["cash"], 225)
        self.assertAlmostEqual(self.agent()["positions"]["BTC/USD"]["qty"], .25)

    def test_crypto_sell_retains_owned_nine_decimal_remainder(self):
        self.buy()
        order = self.engine.reserve_order("openai", "BTC/USD", "sell", 100, qty=.249999995)
        self.engine.update_order(order["id"], {"id": "crypto-sell", "status": "filled",
                                              "filled_qty": ".249999995", "filled_avg_price": "100"})
        self.assertAlmostEqual(self.agent()["positions"]["BTC/USD"]["qty"], 5e-9, places=12)
        with self.assertRaisesRegex(EngineError, "Sell quantity exceeds"):
            self.engine.reserve_order("openai", "BTC/USD", "sell", 100, qty=7e-9)

    def test_posted_coin_fee_uses_activity_price_and_survives_reload_once(self):
        self.buy()
        self.engine.mark({"BTC/USD": 200}, "2026-09-29T20:00:00Z", "test")
        fee = {"id": "20260929000000000::fee-one", "activity_type": "CFEE", "symbol": "BTCUSD",
               "qty": "-0.000625", "net_amount": "0", "price": "120", "status": "executed"}
        self.assertTrue(self.engine.apply_crypto_fee("openai", fee))
        self.assertAlmostEqual(self.agent()["fees"], .075)
        self.assertAlmostEqual(self.agent()["positions"]["BTC/USD"]["qty"], .249375)
        cash = self.agent()["cash"]
        self.engine.close()
        self.engine = Engine(Path(self.tmp.name) / "arena.sqlite3")
        self.assertFalse(self.engine.apply_crypto_fee("openai", fee))
        self.assertEqual(self.agent()["cash"], cash)
        self.assertAlmostEqual(self.agent()["fees"], .075)

    def test_unexecuted_fee_does_not_change_balance_or_deduplication_state(self):
        self.buy()
        before = self.engine.snapshot()
        with self.assertRaisesRegex(EngineError, "has not executed"):
            self.engine.apply_crypto_fee("openai", {"id": "fee-pending", "activity_type": "CFEE",
                                                    "symbol": "BTCUSD", "qty": "-.000625", "status": "pending"})
        self.assertEqual(self.engine.snapshot(), before)

    def test_pending_crypto_notional_is_not_discounted_by_stock_fee(self):
        self.engine.reserve_order("openai", "BTC/USD", "buy", 100, notional=100)
        with self.assertRaisesRegex(EngineError, "exposure cap"):
            self.engine.reserve_order("openai", "SPY", "buy", 100, notional=30)

    def test_pending_stock_fee_is_excluded_from_crypto_exposure(self):
        self.engine.reserve_order("openai", "SPY", "buy", 100, notional=100)
        order = self.engine.reserve_order("openai", "BTC/USD", "buy", 100, notional=25)
        self.assertEqual(order["reserved"], 25)


class CryptoRecoveryTests(unittest.TestCase):
    def test_legacy_and_current_crypto_inventory_match_with_nine_decimal_quantity(self):
        local = {"BTC/USD": {"qty": 5e-9}}
        _inventory_matches([{"symbol": "BTCUSD", "qty": "0.000000005"}], local)
        _inventory_matches([{"symbol": "BTC/USD", "qty": "0.000000005", "asset_class": "crypto"}], local)
        with self.assertRaisesRegex(EngineError, "inventory differs"):
            _inventory_matches([{"symbol": "BTCUSD", "qty": "0.000000007"}], local)

    def test_legacy_crypto_alias_cannot_hide_duplicate_inventory(self):
        with self.assertRaisesRegex(EngineError, "duplicate symbols"):
            _inventory_matches([{"symbol": "BTCUSD", "qty": ".25", "asset_class": "crypto"},
                                {"symbol": "BTC/USD", "qty": ".25", "asset_class": "crypto"}],
                               {"BTC/USD": {"qty": .25}})

    def test_known_legacy_crypto_order_matches_current_ledger_symbol(self):
        known = {"agent_id": "openai", "symbol": "BTC/USD", "side": "buy", "broker_id": "crypto-buy",
                 "status": "filled", "filled_qty": .25, "filled_avg_price": 100}
        service = Mock()
        service.engine.order_by_client_id.return_value = known
        row = {"id": "crypto-buy", "client_order_id": "arena_known", "symbol": "BTCUSD",
               "side": "buy", "status": "filled", "filled_qty": ".25", "filled_avg_price": "100"}
        _orders_match(service, [row], {}, "openai", "arena_original_unknown")
        with self.assertRaisesRegex(EngineError, "known broker order changed"):
            _orders_match(service, [{**row, "filled_qty": ".250000002"}], {}, "openai", "arena_original_unknown")


class CryptoResearchTests(unittest.TestCase):
    def test_signed_crypto_mover_changes_accept_numeric_strings_and_reject_nonfinite_data(self):
        broker = Mock()
        research = AutonomousResearch(broker, [CRYPTO], "2026-09-29T21:00:00Z")
        query = {"kind": "crypto_movers", "query": "", "symbols": [], "lookback_days": 30}
        broker.crypto_movers.return_value = {"gainers": [], "losers": [{"symbol": "BTC/USD", "price": 100,
                                                                      "percent_change": "-1.5"}],
                                             "feed": "crypto_us", "window": "since_previous_completed_utc_day"}
        record = research.run(query)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["data"]["losers"][0]["percent_change"], -1.5)
        for value in (True, "nan", "inf", "-inf"):
            broker.crypto_movers.return_value["losers"][0]["percent_change"] = value
            self.assertEqual(research.run(query)["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
