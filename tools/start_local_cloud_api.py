import argparse
import json
import os
import pathlib
import sys

import boto3


def parse_args():
    parser = argparse.ArgumentParser(description='Start the local API with deployed Lambda environment variables.')
    parser.add_argument('--stack-name', default='transcribe-mvp')
    parser.add_argument('--region', default='us-west-1')
    parser.add_argument('--profile', default='')
    parser.add_argument('--port', type=int, default=3001)
    parser.add_argument('--print-env-json', action='store_true')
    parser.add_argument('--no-run', action='store_true')
    return parser.parse_args()


def load_lambda_environment(stack_name, region, profile):
    session_kwargs = {'region_name': region}
    if profile:
        session_kwargs['profile_name'] = profile
    session = boto3.Session(**session_kwargs)
    cf = session.client('cloudformation')
    lmb = session.client('lambda')
    sts = session.client('sts')
    sts.get_caller_identity()
    resources = cf.describe_stack_resources(StackName=stack_name, LogicalResourceId='ApiFunction')
    function_name = resources['StackResources'][0]['PhysicalResourceId']
    cfg = lmb.get_function_configuration(FunctionName=function_name)
    return function_name, cfg.get('Environment', {}).get('Variables', {})


def main():
    args = parse_args()
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    if args.profile:
        os.environ['AWS_PROFILE'] = args.profile
    os.environ['AWS_SDK_LOAD_CONFIG'] = '1'
    os.environ['AWS_DEFAULT_REGION'] = args.region
    os.environ['AWS_REGION'] = args.region
    os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'

    function_name, variables = load_lambda_environment(args.stack_name, args.region, args.profile)
    for key, value in variables.items():
        os.environ[key] = str(value)

    os.environ['LOCAL_API_MODE'] = 'cloud'
    os.environ['APP_ENV'] = 'local'
    os.environ['LOCAL_DEV_MODE'] = 'true'
    os.environ['LOCAL_API_PORT'] = str(args.port)
    os.environ['LOCAL_STACK_NAME'] = args.stack_name
    os.environ['LOCAL_REGION'] = args.region
    os.environ['LOCAL_AWS_PROFILE'] = args.profile or os.environ.get('AWS_PROFILE', '')
    os.environ['AWS_LAMBDA_FUNCTION_NAME'] = function_name

    payload = {
        'ok': True,
        'mode': 'cloud',
        'port': args.port,
        'stackName': args.stack_name,
        'region': args.region,
        'profile': args.profile or os.environ.get('AWS_PROFILE', ''),
        'apiFunction': function_name,
        'requiredEnv': {
            'TRANSCRIPTS_TABLE': os.environ.get('TRANSCRIPTS_TABLE'),
            'UPLOADS_BUCKET': os.environ.get('UPLOADS_BUCKET'),
            'ARTIFACTS_BUCKET': os.environ.get('ARTIFACTS_BUCKET'),
        },
    }
    if args.print_env_json:
        print(json.dumps(payload), flush=True)
    if args.no_run:
        return

    sys.path.insert(0, str(repo_root / 'backend' / 'src'))
    import local_server

    print(json.dumps(payload), flush=True)
    local_server.main()


if __name__ == '__main__':
    main()
