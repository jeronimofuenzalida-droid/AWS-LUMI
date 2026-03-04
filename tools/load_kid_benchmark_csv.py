import argparse
import csv
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'backend' / 'src'))

from sql_store import ensure_schema, sql_enabled, upsert_kid_benchmark_rows  # noqa: E402


def parse_csv(csv_path: Path):
    with csv_path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        raise RuntimeError('CSV is empty')

    header = [str(h).strip() for h in rows[0]]
    if len(header) < 2:
        raise RuntimeError('CSV header is invalid')

    age_columns = []
    for idx, cell in enumerate(header[1:], start=1):
        if not cell or cell.lower() == 'grand total':
            continue
        try:
            age_columns.append((idx, int(cell)))
        except Exception:
            continue
    if not age_columns:
        raise RuntimeError('No numeric age-month columns found')

    out_rows = []
    per_age_totals = {age: 0 for _, age in age_columns}
    skipped_blank = 0
    for row in rows[1:]:
        if not row:
            continue
        first = str(row[0]).strip() if len(row) > 0 else ''
        if first == '':
            continue
        try:
            unique_word_count = int(first)
        except Exception:
            continue
        for idx, age in age_columns:
            raw = str(row[idx]).strip() if idx < len(row) and row[idx] is not None else ''
            if raw == '':
                skipped_blank += 1
                continue
            try:
                count = int(float(raw))
            except Exception:
                continue
            if count <= 0:
                continue
            out_rows.append(
                {
                    'age_months': age,
                    'unique_word_count': unique_word_count,
                    'kid_count': count,
                }
            )
            per_age_totals[age] += count

    ages = [age for _, age in age_columns]
    return {
        'rows': out_rows,
        'age_columns': sorted(ages),
        'min_age': min(ages),
        'max_age': max(ages),
        'per_age_totals': per_age_totals,
        'skipped_blank_cells': skipped_blank,
        'source_row_count': max(0, len(rows) - 1),
    }


def main():
    parser = argparse.ArgumentParser(description='Load kid benchmark CSV into Aurora SQL')
    parser.add_argument('--csv', default='files/unique_words_per month.csv', help='Path to CSV file')
    parser.add_argument('--dry-run', action='store_true', help='Parse and print summary only')
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise RuntimeError(f'CSV file not found: {csv_path}')

    parsed = parse_csv(csv_path)
    summary = {
        'csv': str(csv_path),
        'ageRange': [parsed['min_age'], parsed['max_age']],
        'ageColumns': parsed['age_columns'],
        'rowsPrepared': len(parsed['rows']),
        'sourceDataRows': parsed['source_row_count'],
        'skippedBlankCells': parsed['skipped_blank_cells'],
        'perAgeTotals': parsed['per_age_totals'],
    }
    if args.dry_run:
        print(json.dumps({'ok': True, 'dryRun': True, **summary}, indent=2))
        return

    if not sql_enabled():
        raise RuntimeError('SQL env not configured: set SQL_CLUSTER_ARN/SQL_SECRET_ARN/SQL_DATABASE or LOCAL_POSTGRES_URL')
    ensure_schema()
    upsert_kid_benchmark_rows(parsed['rows'], source_file=csv_path.name)
    print(json.dumps({'ok': True, 'dryRun': False, **summary}, indent=2))


if __name__ == '__main__':
    main()
