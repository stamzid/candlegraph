#!/usr/bin/env python3

import unittest
from datetime import datetime, timezone

from candlegraph.helpers import add_symbol, format_chart_data


class TestHelpers(unittest.TestCase):
    def test_add_symbol(self):
        data = [
            {"id": "0x123-456", "periodStartUnix": 1609459200, "open": "500", "close": "600"}
        ]
        tokens = {"0x123": "ETH"}

        expected = [
            {"id": "0x123", "symbol": "ETH", "periodStartUnix": 1609459200,
             "timestamp": "2021-01-01T00:00:00", "open": "500", "close": "600"}
        ]

        add_symbol(data, tokens)

        self.assertEqual(data, expected)

    def test_format_chart_data(self):
        data = [
            {
                'interval_start': datetime(2024, 5, 7, 23, 0, tzinfo=timezone.utc),
                'opens': ['500.0'],
                'closes': ['550.0'],
                'max_high': '600.0',
                'min_low': '450.0',
                'avg_price_usd': '525.0'
            },
            {
                'interval_start': datetime(2024, 5, 8, 0, 0, tzinfo=timezone.utc),
                'opens': ['510.0'],
                'closes': ['560.0'],
                'max_high': '610.0',
                'min_low': '460.0',
                'avg_price_usd': '535.0'
            }
        ]

        # Expected output format
        expected = [
            [['2024-05-07T23:00:00', 'open', '500.0'], ['2024-05-08T00:00:00', 'open', '510.0']],
            [['2024-05-07T23:00:00', 'close', '550.0'], ['2024-05-08T00:00:00', 'close', '560.0']],
            [['2024-05-07T23:00:00', 'high', '600.0'], ['2024-05-08T00:00:00', 'high', '610.0']],
            [['2024-05-07T23:00:00', 'low', '450.0'], ['2024-05-08T00:00:00', 'low', '460.0']],
            [['2024-05-07T23:00:00', 'priceUSD', '525.0'], ['2024-05-08T00:00:00', 'priceUSD', '535.0']]
        ]

        result = format_chart_data(data)
        self.assertEqual(result, expected)


if __name__ == '__main__':
    unittest.main()
