#!/usr/bin/env python3
"""Upload Factorio saves to Google Drive using a configured rclone remote."""
import argparse
import csv
import io
import hashlib
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile


def default_saves():
    if sys.platform == 'win32':
        return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'Factorio/saves'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/factorio/saves'
    return Path.home() / '.factorio/saves'


def signature(path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_ctime_ns


def snapshot(source, destination, settle):
    """Never upload the live file: check stability, copy, then validate ZIP CRCs."""
    before = signature(source)
    if before[0] == 0:
        raise ValueError('Пустое сохранение')
    time.sleep(settle)
    if signature(source) != before:
        raise ValueError('Factorio ещё записывает сохранение')
    shutil.copyfile(source, destination)
    if signature(source) != before:
        raise ValueError('Сохранение изменилось во время копирования')
    with zipfile.ZipFile(destination) as archive:
        if not archive.namelist() or archive.testzip() is not None:
            raise ValueError('Ошибка проверки ZIP')
    digest = hashlib.sha256()
    with destination.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def scan(saves, remote, uploaded, settle, timeout):
    failures = 0
    for source in sorted(saves.glob('*.zip')):
        if not source.is_file():
            continue
        try:
            with tempfile.TemporaryDirectory(prefix='factorio-backup-') as temporary:
                copy = Path(temporary) / source.name
                digest = snapshot(source, copy, settle)
                if uploaded.get(source.name) == digest:
                    continue
                subprocess.run(
                    ['rclone', 'copyto', str(copy), remote.rstrip('/') + '/' + source.name,
                     '--checksum', '--retries', '3', '--contimeout', '20s', '--timeout', '2m'],
                    check=True, timeout=timeout,
                )
                # Mark complete only after rclone reports success; retry failures next scan.
                uploaded[source.name] = digest
                logging.info('Загружено: %s', source.name)
        except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
            failures += 1
            logging.warning('%s: %s. Повтор при следующей проверке.', source.name, error)
    return failures


def game_running(process_name):
    """Match executable names, not localized window titles or Steam launchers."""
    if sys.platform == 'win32':
        result = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {process_name}', '/FO', 'CSV', '/NH'],
            check=True, capture_output=True, timeout=15,
        )
        # Executable names are ASCII; localized tasklist notices are irrelevant.
        rows = csv.reader(io.StringIO(result.stdout.decode('utf-8', errors='replace')))
        return any(row and row[0].casefold() == process_name.casefold() for row in rows)
    result = subprocess.run(['ps', '-A', '-o', 'comm='], check=True,
                            capture_output=True, text=True, timeout=15)
    return any(Path(line.strip()).name.casefold() == process_name.casefold()
               for line in result.stdout.splitlines())


def watch(args):
    uploaded = {}
    was_running = False
    pending = False
    logging.info('Ожидание запуска и закрытия %s. Остановка: Ctrl+C', args.process_name)
    while True:
        try:
            running = game_running(args.process_name)
        except (OSError, subprocess.SubprocessError) as error:
            # Failure to inspect processes must never be mistaken for game exit.
            logging.warning('Не удалось проверить процесс: %s', error)
            time.sleep(args.interval)
            continue
        if running and not was_running:
            logging.info('Factorio запущена. Ожидание завершения игры.')
        if was_running and not running:
            pending = True
            logging.info('Factorio закрыта. Загрузка сохранений.')
        was_running = running
        if pending and not running:
            try:
                subprocess.run(['rclone', 'mkdir', args.remote], check=True, timeout=60)
                errors = scan(args.saves, args.remote, uploaded, args.settle, args.timeout)
            except (OSError, subprocess.SubprocessError) as error:
                logging.warning('Google Диск недоступен: %s. Загрузка будет повторена.', error)
                errors = 1
            if args.once:
                return int(bool(errors))
            pending = bool(errors)
            if not pending:
                logging.info('Бэкап завершён. Ожидание следующего запуска игры.')
        time.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(description='Автокопирование сохранений Factorio в Google Диск')
    parser.add_argument('--saves', type=Path, default=default_saves(), help='Папка saves')
    parser.add_argument('--remote', default='gdrive:Factorio Backups', help='Путь rclone')
    parser.add_argument('--interval', type=float, default=2, help='Пауза между проверками процесса, секунд')
    parser.add_argument('--settle', type=float, default=2, help='Проверка завершения записи, секунд')
    parser.add_argument('--timeout', type=float, default=1800, help='Общий лимит загрузки одного файла, секунд')
    parser.add_argument('--once', action='store_true', help='Дождаться закрытия игры, загрузить бэкап и выйти')
    parser.add_argument('--process-name', default='factorio.exe' if sys.platform == 'win32' else 'factorio',
                        help='Имя исполняемого файла игры (не заголовок окна)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    if args.interval <= 0 or args.settle < 0 or args.timeout <= 0:
        parser.error('Интервал и лимит должны быть положительными; settle неотрицательным')
    if not shutil.which('rclone'):
        parser.error('Установите rclone: https://rclone.org/downloads/ и добавьте его в PATH')
    args.saves = args.saves.expanduser().resolve()
    if not args.saves.is_dir():
        parser.error(f'Папка не найдена: {args.saves}. Укажите --saves "путь к saves"')
    if ':' not in args.remote or args.remote.startswith(':'):
        parser.error('Укажите настроенный remote, например gdrive:Factorio Backups')
    logging.info('Папка: %s → %s', args.saves, args.remote)
    return watch(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nОстановлено.')
