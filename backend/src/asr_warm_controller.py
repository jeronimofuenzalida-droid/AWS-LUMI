import json
import os
import time
from datetime import datetime, timezone

import boto3

ddb = boto3.resource('dynamodb')
ecs = boto3.client('ecs')
sqs = boto3.client('sqs')
autoscaling = boto3.client('autoscaling')

ASR_WARM_STATE_TABLE = os.environ.get('ASR_WARM_STATE_TABLE') or ''
ASR_WARM_SCOPE = 'global'
ECS_CLUSTER_ARN = os.environ.get('ECS_CLUSTER_ARN') or ''
ASR_WORKER_SERVICE_NAME = os.environ.get('ASR_WORKER_SERVICE_NAME') or ''
ASR_JOBS_QUEUE_URL = os.environ.get('ASR_JOBS_QUEUE_URL') or ''
ASR_GPU_ONDEMAND_ASG_NAME = os.environ.get('ASR_GPU_ONDEMAND_ASG_NAME') or ''
ASR_GPU_SPOT_ASG_NAME = os.environ.get('ASR_GPU_SPOT_ASG_NAME') or ''


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def to_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def compute_desired_counts(
    *,
    now_epoch,
    warm_until_epoch,
    gpu_warm_until_epoch,
    active_jobs,
    queue_visible,
    queue_not_visible,
    gpu_running_tasks,
    gpu_enabled,
):
    backlog = max(0, int(queue_visible)) + max(0, int(queue_not_visible))
    desired = 1 if (int(now_epoch) < int(warm_until_epoch) or int(active_jobs) > 0 or backlog > 0) else 0
    gpu_desired = 1 if (gpu_enabled and (desired > 0 or int(now_epoch) < int(gpu_warm_until_epoch) or int(gpu_running_tasks) > 0)) else 0
    return {
        'desiredCount': desired,
        'gpuDesiredCount': gpu_desired,
        'backlog': backlog,
    }


def handler(_event, _context):
    if not ASR_WARM_STATE_TABLE or not ECS_CLUSTER_ARN or not ASR_WORKER_SERVICE_NAME or not ASR_JOBS_QUEUE_URL:
        print(
            json.dumps(
                {
                    'event': 'WarmControllerSkipped',
                    'reason': 'missing_config',
                    'table': bool(ASR_WARM_STATE_TABLE),
                    'cluster': bool(ECS_CLUSTER_ARN),
                    'service': bool(ASR_WORKER_SERVICE_NAME),
                    'queue': bool(ASR_JOBS_QUEUE_URL),
                }
            )
        )
        return {'ok': True, 'skipped': True}

    table = ddb.Table(ASR_WARM_STATE_TABLE)
    state = table.get_item(Key={'scope': ASR_WARM_SCOPE}).get('Item') or {}
    warm_until = to_int(state.get('warmUntilEpoch'))
    gpu_warm_until = to_int(state.get('gpuWarmUntilEpoch'))
    active_jobs = max(0, to_int(state.get('activeJobs')))
    now_epoch = int(time.time())

    queue_attrs = sqs.get_queue_attributes(
        QueueUrl=ASR_JOBS_QUEUE_URL,
        AttributeNames=['ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible'],
    ).get('Attributes', {})
    visible = to_int(queue_attrs.get('ApproximateNumberOfMessages'))
    not_visible = to_int(queue_attrs.get('ApproximateNumberOfMessagesNotVisible'))
    decisions = compute_desired_counts(
        now_epoch=now_epoch,
        warm_until_epoch=warm_until,
        gpu_warm_until_epoch=gpu_warm_until,
        active_jobs=active_jobs,
        queue_visible=visible,
        queue_not_visible=not_visible,
        gpu_running_tasks=0,
        gpu_enabled=bool(ASR_GPU_ONDEMAND_ASG_NAME),
    )
    desired = decisions['desiredCount']

    svc = ecs.describe_services(cluster=ECS_CLUSTER_ARN, services=[ASR_WORKER_SERVICE_NAME]).get('services', [])
    current_desired = to_int((svc[0] if svc else {}).get('desiredCount'))

    if current_desired != desired:
        ecs.update_service(
            cluster=ECS_CLUSTER_ARN,
            service=ASR_WORKER_SERVICE_NAME,
            desiredCount=desired,
        )
        print(
            json.dumps(
                {
                    'event': 'WarmScaleUp' if desired == 1 else 'WarmScaleDown',
                    'previousDesired': current_desired,
                    'newDesired': desired,
                    'warmUntilEpoch': warm_until,
                    'activeJobs': active_jobs,
                    'queueVisible': visible,
                    'queueNotVisible': not_visible,
                }
            )
        )

    gpu_ec2_running_tasks = 0
    try:
        if ECS_CLUSTER_ARN:
            task_arns = ecs.list_tasks(cluster=ECS_CLUSTER_ARN, desiredStatus='RUNNING').get('taskArns') or []
            if task_arns:
                for i in range(0, len(task_arns), 100):
                    desc = ecs.describe_tasks(cluster=ECS_CLUSTER_ARN, tasks=task_arns[i:i + 100]).get('tasks', [])
                    for task in desc:
                        if (task.get('launchType') or '').upper() == 'EC2' and (task.get('lastStatus') or '').upper() == 'RUNNING':
                            gpu_ec2_running_tasks += 1
    except Exception as e:
        print(json.dumps({'event': 'GpuWarmTaskCountFailed', 'error': str(e)}))

    decisions = compute_desired_counts(
        now_epoch=now_epoch,
        warm_until_epoch=warm_until,
        gpu_warm_until_epoch=gpu_warm_until,
        active_jobs=active_jobs,
        queue_visible=visible,
        queue_not_visible=not_visible,
        gpu_running_tasks=gpu_ec2_running_tasks,
        gpu_enabled=bool(ASR_GPU_ONDEMAND_ASG_NAME),
    )
    gpu_desired = decisions['gpuDesiredCount']
    current_gpu_desired = None
    if ASR_GPU_ONDEMAND_ASG_NAME:
        try:
            groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASR_GPU_ONDEMAND_ASG_NAME]).get('AutoScalingGroups', [])
            current_gpu_desired = to_int((groups[0] if groups else {}).get('DesiredCapacity'))
        except Exception as e:
            print(json.dumps({'event': 'GpuWarmDescribeFailed', 'asg': ASR_GPU_ONDEMAND_ASG_NAME, 'error': str(e)}))
        if current_gpu_desired is not None and current_gpu_desired != gpu_desired:
            try:
                autoscaling.update_auto_scaling_group(
                    AutoScalingGroupName=ASR_GPU_ONDEMAND_ASG_NAME,
                    DesiredCapacity=gpu_desired,
                )
                print(
                    json.dumps(
                        {
                            'event': 'GpuWarmScaleUp' if gpu_desired == 1 else 'GpuWarmScaleDown',
                            'asg': ASR_GPU_ONDEMAND_ASG_NAME,
                            'previousDesired': current_gpu_desired,
                            'newDesired': gpu_desired,
                            'gpuWarmUntilEpoch': gpu_warm_until,
                            'gpuEc2RunningTasks': gpu_ec2_running_tasks,
                        }
                    )
                )
            except Exception as e:
                print(json.dumps({'event': 'GpuWarmScaleFailed', 'asg': ASR_GPU_ONDEMAND_ASG_NAME, 'desired': gpu_desired, 'error': str(e)}))

    # Queue-service GPU mode should keep Spot ASG drained to avoid orphaned costs.
    if ASR_GPU_SPOT_ASG_NAME:
        try:
            spot_groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[ASR_GPU_SPOT_ASG_NAME]).get('AutoScalingGroups', [])
            current_spot_desired = to_int((spot_groups[0] if spot_groups else {}).get('DesiredCapacity'))
            if current_spot_desired != 0:
                autoscaling.update_auto_scaling_group(
                    AutoScalingGroupName=ASR_GPU_SPOT_ASG_NAME,
                    DesiredCapacity=0,
                )
                print(
                    json.dumps(
                        {
                            'event': 'GpuSpotForceScaleDown',
                            'asg': ASR_GPU_SPOT_ASG_NAME,
                            'previousDesired': current_spot_desired,
                            'newDesired': 0,
                        }
                    )
                )
        except Exception as e:
            print(json.dumps({'event': 'GpuSpotForceScaleDownFailed', 'asg': ASR_GPU_SPOT_ASG_NAME, 'error': str(e)}))

    print(
        json.dumps(
            {
                'event': 'QueueBacklog',
                'queueVisible': visible,
                'queueNotVisible': not_visible,
                'activeJobs': active_jobs,
                'warmUntilEpoch': warm_until,
                'gpuWarmUntilEpoch': gpu_warm_until,
                'gpuEc2RunningTasks': gpu_ec2_running_tasks,
                'gpuDesiredCount': gpu_desired,
                'desiredCount': desired,
                'nowEpoch': now_epoch,
            }
        )
    )

    table.update_item(
        Key={'scope': ASR_WARM_SCOPE},
        UpdateExpression='SET updatedAt = :u, lastEvaluatedAt = :u, lastDesiredCount = :d, queueVisible = :v, queueNotVisible = :n, gpuLastDesiredCount = :gd, gpuEc2RunningTasks = :gt',
        ExpressionAttributeValues={':u': now_iso(), ':d': desired, ':v': visible, ':n': not_visible, ':gd': gpu_desired, ':gt': gpu_ec2_running_tasks},
    )
    return {'ok': True, 'desiredCount': desired, 'gpuDesiredCount': gpu_desired}
