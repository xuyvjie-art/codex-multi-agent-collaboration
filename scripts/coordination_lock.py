#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Cooperative, local single-writer lock with explicit, audited recovery."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import secrets
import signal
import stat
import subprocess
import sys
import time

MARKER = 'writer.lock'
GATE = '.writer.gate'
AUDIT = 'writer.audit.jsonl'
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
DIRECTORY = getattr(os, 'O_DIRECTORY', 0)

class LockError(Exception):
    pass

def directory(path):
    """Open every directory component without following symbolic links."""
    if not os.path.isabs(path) or '..' in path.split('/'):
        raise LockError('lock directory must be an absolute path without ..')
    fd = os.open('/', os.O_RDONLY | DIRECTORY)
    try:
        for part in path.split('/'):
            if not part or part == '.':
                continue
            nxt = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        s = os.fstat(fd)
        if s.st_uid != os.getuid() or s.st_mode & 0o022:
            raise LockError('lock directory must be owned by the current user and not group/world writable')
        return fd
    except BaseException:
        os.close(fd)
        raise

def safe_open(dfd, name, flags, mode=0o600):
    fd = os.open(name, flags | NOFOLLOW, mode, dir_fd=dfd)
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_nlink != 1:
        os.close(fd)
        raise LockError(f'unsafe file: {name}')
    return fd

@contextlib.contextmanager
def gate(dfd):
    fd = safe_open(dfd, GATE, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)

def read_marker(dfd):
    try:
        fd = safe_open(dfd, MARKER, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        s = os.fstat(fd)
        if s.st_size > 65536:
            raise LockError('marker exceeds size limit')
        data = b''
        while len(data) <= 65536:
            chunk = os.read(fd, 65537 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > 65536:
            raise LockError('marker exceeds size limit')
        try:
            record = json.loads(data)
            if not isinstance(record, dict):
                record = None
        except (ValueError, UnicodeDecodeError):
            record = None
        return {'record': record, 'sha256': hashlib.sha256(data).hexdigest(), 'inode': s.st_ino, 'device': s.st_dev}
    finally:
        os.close(fd)

def audit(dfd, event, **fields):
    entry = {'event': event, 'at': timestamp(), **fields}
    data = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n').encode()
    fd = safe_open(dfd, AUDIT, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        while data:
            n = os.write(fd, data)
            if not n:
                raise OSError('short audit write')
            data = data[n:]
        os.fsync(fd)
    finally:
        os.close(fd)

def timestamp(epoch=None):
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch, dt.timezone.utc).isoformat()

def remove_checked(dfd, marker):
    s = os.stat(MARKER, dir_fd=dfd, follow_symlinks=False)
    if (s.st_dev, s.st_ino) != (marker['device'], marker['inode']) or not stat.S_ISREG(s.st_mode):
        raise LockError('marker changed; refusing removal')
    os.unlink(MARKER, dir_fd=dfd)
    os.fsync(dfd)

def acquire(dfd, owner, resource, ttl):
    if not owner.strip() or not resource.strip() or not math.isfinite(ttl) or ttl <= 0:
        raise LockError('owner, resource and positive TTL are required')
    now = time.time()
    record = {'version': 1, 'owner': owner, 'resource': resource, 'token': secrets.token_urlsafe(32), 'pid': os.getpid(), 'created_at': timestamp(now), 'expires_at': timestamp(now + ttl), 'expires_epoch': now + ttl}
    with gate(dfd):
        if read_marker(dfd) is not None:
            raise LockError('writer lock is already held, including if expired')
        try:
            fd = safe_open(dfd, MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            raise LockError('writer lock already exists')
        try:
            data = (json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode()
            while data:
                n = os.write(fd, data)
                if not n:
                    raise OSError('short marker write')
                data = data[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(dfd)
        audit(dfd, 'acquire', owner=owner, resource=resource, marker_sha256=read_marker(dfd)['sha256'])
    return record

def release(dfd, token):
    with gate(dfd):
        marker = read_marker(dfd)
        record = marker and marker['record']
        if not record or not isinstance(record.get('token'), str) or not secrets.compare_digest(record['token'], token):
            raise LockError('lock absent, unreadable or token does not match')
        audit(dfd, 'release', owner=record.get('owner'), resource=record.get('resource'), marker_sha256=marker['sha256'])
        remove_checked(dfd, marker)

def recover(dfd, expected_hash, reason, confirmed):
    if not confirmed or not reason.strip():
        raise LockError('recovery requires explicit confirmation and a nonempty reason')
    with gate(dfd):
        marker = read_marker(dfd)
        if not marker or not secrets.compare_digest(marker['sha256'], expected_hash):
            raise LockError('marker absent or changed since inspection')
        record = marker['record']
        if record:
            expiry = record.get('expires_epoch')
            if not isinstance(expiry, (int, float)) or time.time() < expiry:
                raise LockError('lock is not expired or expiry is invalid; do not recover automatically')
        # An incomplete marker from a crash has no trustworthy expiry; explicit
        # human confirmation and its exact inspected digest are required.
        audit(dfd, 'recover', reason=reason, confirmed_writer_stopped=True, previous={k: v for k, v in record.items() if k != 'token'} if record else None, marker_sha256=marker['sha256'])
        remove_checked(dfd, marker)

def run_session(dfd, args):
    record = acquire(dfd, args.owner, args.resource, args.ttl)
    child = None
    received = []
    previous = {}
    def forward(signum, _frame):
        received.append(signum)
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, forward)
        if received:
            return 128 + received[0]
        child = subprocess.Popen(args.command, start_new_session=True)
        if received:
            forward(received[0], None)
        result = child.wait()
        return 128 + received[0] if received else (128 - result if result < 0 else result)
    finally:
        # Never unlock while the immediate command is still running.
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child is not None:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise LockError('command process group still exists; lock retained for explicit recovery')
        release(dfd, record['token'])

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dir', required=True, help='existing private absolute lock directory')
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    for name in ('acquire', 'run'):
        q = sub.add_parser(name)
        q.add_argument('--owner', required=True)
        q.add_argument('--resource', required=True)
        q.add_argument('--ttl', type=float, required=True, help='seconds; expiration never removes the lock')
        if name == 'run':
            q.add_argument('command', nargs=argparse.REMAINDER)
    q = sub.add_parser('release'); q.add_argument('--token', required=True)
    q = sub.add_parser('recover'); q.add_argument('--sha256', required=True); q.add_argument('--reason', required=True); q.add_argument('--confirm-writer-stopped', action='store_true')
    args = p.parse_args(argv)
    try:
        dfd = directory(args.dir)
        try:
            if args.action == 'status':
                marker = read_marker(dfd)
                if marker and marker['record'] is not None:
                    marker['record'] = {k: v for k, v in marker['record'].items() if k != 'token'}
                print(json.dumps({'locked': marker is not None, 'marker': marker}, ensure_ascii=False, sort_keys=True))
            elif args.action == 'acquire':
                print(json.dumps(acquire(dfd, args.owner, args.resource, args.ttl), ensure_ascii=False))
            elif args.action == 'release':
                release(dfd, args.token)
            elif args.action == 'recover':
                recover(dfd, args.sha256, args.reason, args.confirm_writer_stopped)
            else:
                if args.command and args.command[0] == '--':
                    args.command.pop(0)
                if not args.command:
                    raise LockError('run requires a command after --')
                return run_session(dfd, args)
        finally:
            os.close(dfd)
        return 0
    except (LockError, OSError, ValueError) as e:
        print(f'coordination lock: {e}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
