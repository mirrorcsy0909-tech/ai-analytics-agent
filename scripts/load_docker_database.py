"""Copy a consistent local SQLite snapshot to a named Docker data volume.

No bind mounts, credentials, cloud calls or changes to the source database.
Stop API/UI containers before replacing their database. Requires a built image.
"""

import argparse
import contextlib
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
VOLUME = 'ai-analytics-agent-data'
IMAGE = 'ai-analytics-agent:local'


def load_database(source, *, volume=VOLUME, image=IMAGE):
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError('Source database is missing.')
    active = subprocess.run(['docker', 'ps', '--filter', f'volume={volume}', '--format', '{{.ID}}'],
                            check=True, capture_output=True, text=True)
    if active.stdout.strip():
        raise RuntimeError('Stop containers using this data volume before loading a snapshot.')
    with tempfile.TemporaryDirectory(prefix='analytics-docker-data-') as temp:
        snapshot = Path(temp)/'analytics.db'
        with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as original:
            with contextlib.closing(sqlite3.connect(snapshot)) as target:
                original.backup(target)
                # Verify the expected table before changing the Docker volume.
                target.execute('SELECT 1 FROM transactions LIMIT 1').fetchone()
        subprocess.run(['docker', 'volume', 'create', volume], check=True, stdout=subprocess.DEVNULL)
        # Transfer over local Docker stdin rather than sharing a host directory.
        program = (
            'import os,sys; from pathlib import Path; '
            'p=Path("/data/analytics.db.pending"); '
            'p.write_bytes(sys.stdin.buffer.read()); '
            'p.chmod(0o644); os.replace(p,"/data/analytics.db")'
        )
        with snapshot.open('rb') as stream:
            subprocess.run(['docker', 'run', '--rm', '--user', '0', '-i',
                            '--mount', f'type=volume,source={volume},target=/data',
                            '--entrypoint', 'python', image, '-c', program], stdin=stream, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=BASE_DIR/'database/analytics.db')
    parser.add_argument('--volume', default=VOLUME)
    parser.add_argument('--image', default=IMAGE)
    args = parser.parse_args()
    try:
        load_database(args.database, volume=args.volume, image=args.image)
    except (OSError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(f'Database snapshot was not loaded ({type(error).__name__}). Check the source, Docker and running containers.')
        return 1
    print('Database snapshot loaded into the local Docker volume; source unchanged.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
