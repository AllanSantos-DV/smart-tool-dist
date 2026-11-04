"""Removes the Camoufox builds older than the active one after a verified update. camoufox 0.5.6 installs each build in
its own folder under `browsers/<repo>/` and never deletes the previous ones, so the cache grows by about 2.5 GB per
build. Nothing is removed while the user pinned a build (`camoufox set` writes `pinned` in config.json), and only the
active build's own repository is pruned: the active build and any newer one stay. Each folder is first renamed to a
hidden name, which Windows refuses while any file inside is open, so a build in use is skipped whole instead of left
half removed; a hidden folder left by an interrupted deletion is removed on the next run."""
import os
import shutil
import time

REMOVING_PREFIX = ".removing-"


def folder_size(path):
    """Bytes of the files under path."""
    return sum(os.path.getsize(os.path.join(root, name)) for root, _dirs, names in os.walk(path) for name in names)


def _delete(build, browsers_dir):
    """Renames the build's folder to a hidden one in browsers_dir and deletes it; the size freed, or None when the
    rename was refused because a file inside is in use."""
    name = f"{build.repo_name}/{build.path.name}"
    size = folder_size(build.path)
    hidden = browsers_dir / f"{REMOVING_PREFIX}{build.repo_name}-{build.path.name}-{time.time_ns()}"
    try:
        os.rename(build.path, hidden)
    except PermissionError as exc:
        print(f"Kept Camoufox build {name}: in use ({exc.strerror})", flush=True)
        return None
    shutil.rmtree(hidden)
    print(f"Removed old Camoufox build {name} ({size / 2**20:.0f} MB)", flush=True)
    return size


def prune(installed, config, browsers_dir):
    """Deletes the builds in installed (camoufox `InstalledVersion`s) of the active build's repository whose version is
    older than the active one, unless config pins a build, and returns the bytes freed."""
    if config.get("pinned"):
        print(f"Camoufox build pinned to {config['pinned']}: old builds kept", flush=True)
        return 0
    freed = 0
    for leftover in browsers_dir.glob(f"{REMOVING_PREFIX}*"):
        size = folder_size(leftover)
        shutil.rmtree(leftover)
        freed += size
        print(f"Removed an interrupted deletion: {leftover.name} ({size / 2**20:.0f} MB)", flush=True)
    active = next((build for build in installed if build.is_active), None)
    if active is None:
        print("No active Camoufox build: old builds kept", flush=True)
        return freed
    for build in installed:
        if build.repo_name == active.repo_name and build.version < active.version:
            freed += _delete(build, browsers_dir) or 0
    print(f"Old Camoufox builds pruned: {freed / 2**30:.2f} GB freed; active build "
          f"{active.repo_name}/{active.path.name} kept", flush=True)
    return freed


def main():
    """Prunes the user's real Camoufox cache, read through camoufox's own multiversion manager."""
    from camoufox import multiversion
    prune(multiversion.list_installed(), multiversion.load_config(), multiversion.BROWSERS_DIR)


if __name__ == "__main__":
    main()
