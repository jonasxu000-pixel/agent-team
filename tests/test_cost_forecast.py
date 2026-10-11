"""Cost planning regressions; no network, credentials or paid inference."""
import copy
import time
import unittest
from types import SimpleNamespace

from agent_team.cost_monitor import CostMonitor
from agent_team.workflow import DEFAULTS


class CostForecastTests(unittest.TestCase):
    def setUp(self):
        self.monitor = CostMonitor(SimpleNamespace(calls=lambda: []))
        self.config = dict(DEFAULTS, claude_output_tokens=4000, claude_context_bytes=48000,
                           channel_ratio_reference=2.75, reservation_margin=1.25)
        self.snapshot = {'configured_model': 'fixture-sonnet', 'quota_per_usd': 500000,
            'provider_scope': 'fixture', 'records': [{'model': 'fixture-sonnet', 'created_at': time.time(),
                'success': True, 'input_tokens': 1000, 'cache_write_tokens': 1000, 'cache_read_tokens': 8000,
                'pricing': {'model_ratio': 1, 'completion_ratio': 5, 'group_ratio': 2.75,
                            'cache_ratio': .1, 'cache_creation_ratio': 1.25}}]}

    def quote(self, prompt='Review source and edit the assigned UI files.', source_bytes=48000):
        return self.monitor.estimate_specialist(prompt, self.config, self.snapshot,
            role='claude_native_develop', source_bytes=source_bytes)

    def test_source_growth_does_not_multiply_output_allocation(self):
        small, large = self.quote(source_bytes=100), self.quote(source_bytes=48000)
        a, b = small['reservation_breakdown'], large['reservation_breakdown']
        self.assertEqual(a['output_token_allocation'], 6 * 4000)
        self.assertEqual(a['output_client_usd'], b['output_client_usd'])
        self.assertGreater(b['input_client_usd'], a['input_client_usd'])
        self.assertAlmostEqual(b['output_client_usd'], .24)

    def test_short_prompt_cannot_explode_output_cost_via_source_ratio(self):
        quote = self.quote(prompt='UI')
        self.assertEqual(quote['reservation_breakdown']['output_token_allocation'], 24000)
        # 6*(50,002-byte input at $2.5/M + 4,000 output at $10/M), platform x2.75, margin x1.25.
        self.assertAlmostEqual(quote['platform_range_usd'][1], (50002 * 6 * 2.5e-6 + .24) * 2.75 * 1.25)
        self.assertLess(quote['platform_range_usd'][1], 4)

    def test_warm_and_cold_scenarios_are_separate_from_reservation(self):
        quote = self.quote()
        forecast = quote['spend_forecast']
        self.assertEqual(forecast['historical_request_samples'], 1)
        self.assertAlmostEqual(forecast['historical_cache_read_share'], .8)
        self.assertLess(forecast['platform_range_usd'][0], forecast['platform_range_usd'][1])
        self.assertFalse(forecast['cache_hit_guaranteed'])
        self.assertFalse(forecast['used_for_budget_authorization'])
        self.assertEqual(forecast['confidence'], 'LOW')
        self.assertGreater(quote['platform_range_usd'][1], forecast['platform_range_usd'][1])

    def test_unknown_cache_data_does_not_invent_discount(self):
        del self.snapshot['records'][0]['cache_read_tokens']
        forecast = self.quote()['spend_forecast']
        self.assertIsNone(forecast['historical_cache_read_share'])
        self.assertEqual(forecast['historical_request_samples'], 0)
        self.assertEqual(*forecast['platform_range_usd'])

    def test_stale_and_other_model_samples_do_not_affect_cache_scenario(self):
        stale = copy.deepcopy(self.snapshot['records'][0])
        stale.update(created_at=time.time() - 48 * 3600, cache_read_tokens=1000000)
        other = dict(stale, model='different-model', created_at=time.time())
        self.snapshot['records'] += [stale, other]
        forecast = self.quote()['spend_forecast']
        self.assertEqual(forecast['historical_request_samples'], 1)
        self.assertAlmostEqual(forecast['historical_cache_read_share'], .8)

    def test_unverified_prices_remain_unknown(self):
        self.snapshot['records'][0]['created_at'] -= 48 * 3600
        quote = self.quote()
        self.assertEqual(quote['status'], 'UNKNOWN')
        self.assertIsNone(quote['platform_range_usd'])
        self.assertNotIn('spend_forecast', quote)

    def test_manual_prices_do_not_receive_unverified_cache_discount(self):
        self.config['claude_prices'] = {'input': 3, 'output': 15}
        forecast = self.quote()['spend_forecast']
        self.assertEqual(*forecast['platform_range_usd'])
        self.assertIsNone(forecast['historical_cache_read_share'])


if __name__ == '__main__':
    unittest.main()
