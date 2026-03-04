import pathlib
import sys
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

from runtime_config import build_app_config_payload


class RuntimeConfigTests(unittest.TestCase):
    def test_build_app_config_payload_normalizes_types(self):
        payload = build_app_config_payload(
            kid_benchmark_min_months=8,
            kid_benchmark_max_months=30,
            warm_window_seconds=300,
            engine='Whisper',
            dispatch_mode='QUEUE_SERVICE',
            gpu_enabled='',
            gpu_only_pipeline=False,
        )

        self.assertEqual(payload['kidBenchmarkMinMonths'], 8)
        self.assertEqual(payload['kidBenchmarkMaxMonths'], 30)
        self.assertEqual(payload['warmWindowSeconds'], 300)
        self.assertEqual(payload['engine'], 'whisper')
        self.assertEqual(payload['dispatchMode'], 'queue_service')
        self.assertFalse(payload['gpuEnabled'])
        self.assertFalse(payload['gpuOnlyPipeline'])
        self.assertEqual(payload['runtimeStatusSemantics']['cpu'], 'worker_capacity')
        self.assertEqual(payload['runtimeStatusSemantics']['gpu'], 'instances')


if __name__ == '__main__':
    unittest.main()
