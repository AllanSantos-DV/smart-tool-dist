"""Visão Git do índice. Branches isolam manifests; a raiz continua sendo o projeto."""
import contextlib
import contextvars
import hashlib
import os
import shutil
import subprocess
import time

import project_identity

_PINNED = contextvars.ContextVar('smart_index_view', default=None)
_CACHE = {}


class ViewChanged(RuntimeError):
    """Checkout/commit durante uma leitura: não publicar resultado de outra visão."""


def _git_marker(root):
    current = project_identity.display_root(root)
    while True:
        if os.path.lexists(os.path.join(current, '.git')):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _run(root, *args, optional=False):
    result = subprocess.run(['git', '--no-optional-locks', '-C', root, *args],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        if optional and result.returncode in (1, 128):
            return ''
        raise RuntimeError('Could not identify the Git view. Check access to the repository.')
    return result.stdout.decode('utf-8', 'replace').strip()


def _stamp(view):
    paths = [os.path.join(view['git_dir'], name) for name in ('HEAD', 'index', 'logs/HEAD')]
    paths += [os.path.join(view['common_dir'], 'packed-refs')]
    if view.get('ref'):
        paths.append(os.path.join(view['common_dir'], view['ref']))
    result = []
    for path in paths:
        try:
            stat = os.stat(path)
            result.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append(None)
    return tuple(result)


def describe(root, fresh=False):
    root = project_identity.display_root(root)
    key = project_identity.canonical_root(root)
    pinned = _PINNED.get()
    if pinned and pinned['root'] == key and not fresh:
        return pinned
    old = _CACHE.get(key)
    if not fresh and old and time.monotonic()-old[0] < 1:
        return old[1]
    view = {'root': key, 'git': False, 'view_id': 'workspace', 'label': 'Folder without Git',
            'branch': None, 'commit': None}
    if _git_marker(root):
        if not shutil.which('git'):
            raise RuntimeError('This folder contains Git, but the git executable is not available.')
        paths = _run(root, 'rev-parse', '--path-format=absolute', '--show-toplevel', '--absolute-git-dir', '--git-common-dir').splitlines()
        if len(paths) != 3:
            raise RuntimeError('Git did not report the repository directories.')
        view.update(git_dir=paths[1], common_dir=paths[2])
        before = _stamp(view)
        ref = _run(root, 'symbolic-ref', '-q', 'HEAD', optional=True)
        view['ref'] = ref
        ref_before = _stamp(view)
        commit = _run(root, 'rev-parse', '--verify', 'HEAD', optional=True)
        if not ref and not commit:
            raise RuntimeError('Could not identify the Git HEAD.')
        identity = ref or 'detached:' + commit
        view.update(git=True, ref=ref, branch=ref.removeprefix('refs/heads/') if ref else None,
                    commit=commit or None, repo_root=paths[0], git_dir=paths[1], common_dir=paths[2],
                    view_id=hashlib.sha256(identity.encode()).hexdigest()[:16],
                    label=ref.removeprefix('refs/heads/') if ref else 'Detached ' + commit[:10])
        view['stamp'] = _stamp(view)
        if view['stamp'] != ref_before or view['stamp'][:4] != before[:4]:
            raise ViewChanged('Git changed while its view was being identified. Try again after the checkout.')
    _CACHE[key] = (time.monotonic(), view)
    return view


def storage_key(root):
    view = describe(root)
    key = project_identity.project_id(root)
    return key + '.v-' + view['view_id'] if view['git'] else key


def assert_current(view):
    if view.get('git') and _stamp(view) != view.get('stamp'):
        _CACHE.pop(view['root'], None)
        raise ViewChanged('The Git view changed during the operation. The result was discarded; search again on the current branch.')


@contextlib.contextmanager
def pin(root):
    current = _PINNED.get()
    if current and current['root'] == project_identity.canonical_root(root):
        yield current
        return
    view = describe(root, fresh=True)
    token = _PINNED.set(view)
    try:
        yield view
    finally:
        _PINNED.reset(token)


def public(view):
    return {key: view.get(key) for key in ('git', 'view_id', 'label', 'branch', 'commit')}
