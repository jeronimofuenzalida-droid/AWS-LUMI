RUNTIME_STATUS_SEMANTICS = {
    'cpu': 'worker_capacity',
    'gpu': 'instances',
}


def build_app_config_payload(
    *,
    kid_benchmark_min_months,
    kid_benchmark_max_months,
    warm_window_seconds,
    engine,
    dispatch_mode,
    gpu_enabled,
    gpu_only_pipeline,
):
    return {
        'kidBenchmarkMinMonths': int(kid_benchmark_min_months),
        'kidBenchmarkMaxMonths': int(kid_benchmark_max_months),
        'warmWindowSeconds': int(warm_window_seconds),
        'runtimeStatusSemantics': dict(RUNTIME_STATUS_SEMANTICS),
        'engine': str(engine or '').strip().lower(),
        'dispatchMode': str(dispatch_mode or '').strip().lower(),
        'gpuEnabled': bool(gpu_enabled),
        'gpuOnlyPipeline': bool(gpu_only_pipeline),
    }
