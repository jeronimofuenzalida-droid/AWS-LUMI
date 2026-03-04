import json

from sql_store import ensure_schema, sql_enabled


def main():
    if not sql_enabled():
        raise RuntimeError('SQL env not configured: set SQL_CLUSTER_ARN/SQL_SECRET_ARN/SQL_DATABASE or LOCAL_POSTGRES_URL')
    ensure_schema()
    print(json.dumps({'ok': True, 'message': 'SQL schema ensured'}))


if __name__ == '__main__':
    main()
