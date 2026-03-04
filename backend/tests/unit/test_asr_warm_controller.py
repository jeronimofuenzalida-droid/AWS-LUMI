import os
import pathlib
import sys
import unittest


os.environ.setdefault('AWS_DEFAULT_REGION', 'us-west-1')
os.environ.setdefault('AWS_EC2_METADATA_DISABLED', 'true')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'src'))

from asr_warm_controller import compute_desired_counts


class WarmControllerDecisionTests(unittest.TestCase):
    def test_queue_backlog_keeps_cpu_worker_warm(self):
        result = compute_desired_counts(
            now_epoch=1000,
            warm_until_epoch=0,
            gpu_warm_until_epoch=0,
            active_jobs=0,
            queue_visible=1,
            queue_not_visible=0,
            gpu_running_tasks=0,
            gpu_enabled=False,
        )

        self.assertEqual(result['desiredCount'], 1)
        self.assertEqual(result['gpuDesiredCount'], 0)
        self.assertEqual(result['backlog'], 1)

    def test_gpu_window_keeps_gpu_desired_when_enabled(self):
        result = compute_desired_counts(
            now_epoch=1000,
            warm_until_epoch=0,
            gpu_warm_until_epoch=1100,
            active_jobs=0,
            queue_visible=0,
            queue_not_visible=0,
            gpu_running_tasks=0,
            gpu_enabled=True,
        )

        self.assertEqual(result['desiredCount'], 0)
        self.assertEqual(result['gpuDesiredCount'], 1)


if __name__ == '__main__':
    unittest.main()
