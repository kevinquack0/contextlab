"""Descriptor-bound, create-only persistence for immutable research artifacts."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any


class ImmutableIOError(ValueError):
    """An immutable artifact path or payload was unsafe or inconsistent."""


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize the repository's canonical human-readable JSON representation."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _repository_relative(root: Path, path: Path) -> tuple[Path, Path]:
    repository = Path(os.path.abspath(root))
    try:
        metadata = repository.lstat()
    except OSError as exc:
        raise ImmutableIOError("immutable repository root is missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ImmutableIOError("immutable repository root is unsafe")
    requested = path if path.is_absolute() else repository / path
    absolute = Path(os.path.abspath(requested))
    try:
        relative = absolute.relative_to(repository)
    except ValueError as exc:
        raise ImmutableIOError("immutable artifact escapes the repository") from exc
    if not relative.parts or relative.name in {"", ".", ".."} or ".." in relative.parts:
        raise ImmutableIOError("immutable artifact path is invalid")
    return repository, relative


def _open_parent(repository: Path, relative: Path, *, create: bool) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(repository, flags)
    except OSError as exc:
        raise ImmutableIOError("cannot open immutable repository root safely") from exc
    try:
        for component in relative.parent.parts:
            if component in {"", ".", ".."}:
                raise ImmutableIOError("immutable artifact parent is invalid")
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ImmutableIOError("immutable artifact parent is unsafe") from exc
    except Exception:
        os.close(descriptor)
        raise


def _read_regular_at(parent: int, name: str) -> bytes | None:
    try:
        path_stat = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ImmutableIOError("cannot inspect immutable artifact") from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise ImmutableIOError("immutable artifact target is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ImmutableIOError("immutable artifact target is unsafe")
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino)
        if (
            identity != (after.st_dev, after.st_ino)
            or identity != (current.st_dev, current.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise ImmutableIOError("immutable artifact changed while it was read")
        return b"".join(chunks)
    except OSError as exc:
        raise ImmutableIOError("cannot read immutable artifact") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_same_inode(parent: int, name: str, *, device: int, inode: int) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (device, inode):
            os.unlink(name, dir_fd=parent)
    except OSError:
        pass


def read_bytes_snapshot(
    root: Path, path: Path, *, max_bytes: int | None = None
) -> bytes:
    """Read one repository file once through a stable no-follow descriptor."""

    repository, relative = _repository_relative(root, path)
    parent = _open_parent(repository, relative, create=False)
    try:
        data = _read_regular_at(parent, relative.name)
    finally:
        os.close(parent)
    if data is None:
        raise ImmutableIOError("immutable artifact is missing")
    if max_bytes is not None and (max_bytes < 0 or len(data) > max_bytes):
        raise ImmutableIOError("immutable artifact exceeds its size bound")
    return data


def write_bytes_once_or_verify(root: Path, path: Path, payload: bytes) -> bool:
    """Create one immutable regular file atomically, or verify exact saved bytes.

    Returns ``True`` when this call created the destination and ``False`` when an
    identical destination already existed.
    """

    if not isinstance(payload, bytes):
        raise ImmutableIOError("immutable artifact payload must be bytes")
    repository, relative = _repository_relative(root, path)
    parent = _open_parent(repository, relative, create=True)
    name = relative.name
    temporary = f".{name}.contextlab-{secrets.token_hex(12)}.tmp"
    temporary_stat: os.stat_result | None = None
    destination_created = False
    try:
        current = _read_regular_at(parent, name)
        if current is not None:
            if current != payload:
                raise ImmutableIOError("immutable artifact differs")
            return False

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
            temporary_stat = os.fstat(descriptor)
            if not stat.S_ISREG(temporary_stat.st_mode):
                raise ImmutableIOError("immutable temporary target is unsafe")
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("short immutable artifact write")
                written += count
            os.fsync(descriptor)
        except OSError as exc:
            raise ImmutableIOError("cannot write immutable artifact") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if temporary_stat is None:
            raise ImmutableIOError("immutable temporary target is unavailable")
        staged = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if (staged.st_dev, staged.st_ino) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        ):
            raise ImmutableIOError("immutable temporary target changed")
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            destination_created = True
        except FileExistsError:
            concurrent = _read_regular_at(parent, name)
            if concurrent != payload:
                raise ImmutableIOError("concurrent immutable artifact differs")
            return False

        published = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (published.st_dev, published.st_ino) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        ) or not stat.S_ISREG(published.st_mode):
            raise ImmutableIOError("immutable artifact changed during publication")
        _unlink_same_inode(
            parent,
            temporary,
            device=temporary_stat.st_dev,
            inode=temporary_stat.st_ino,
        )
        os.fsync(parent)
        verified = _read_regular_at(parent, name)
        if verified != payload:
            raise ImmutableIOError("immutable artifact changed after publication")
        return True
    except Exception:
        if destination_created and temporary_stat is not None:
            _unlink_same_inode(
                parent,
                name,
                device=temporary_stat.st_dev,
                inode=temporary_stat.st_ino,
            )
            try:
                os.fsync(parent)
            except OSError:
                pass
        raise
    finally:
        if temporary_stat is not None:
            _unlink_same_inode(
                parent,
                temporary,
                device=temporary_stat.st_dev,
                inode=temporary_stat.st_ino,
            )
        os.close(parent)


def replace_bytes_atomically(root: Path, path: Path, payload: bytes) -> None:
    """Atomically replace one regular file through a retained parent descriptor."""

    if not isinstance(payload, bytes):
        raise ImmutableIOError("immutable artifact payload must be bytes")
    repository, relative = _repository_relative(root, path)
    parent = _open_parent(repository, relative, create=True)
    name = relative.name
    temporary = f".{name}.contextlab-{secrets.token_hex(12)}.tmp"
    temporary_stat: os.stat_result | None = None
    replaced = False
    try:
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(current.st_mode):
                raise ImmutableIOError("immutable artifact target is unsafe")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
            temporary_stat = os.fstat(descriptor)
            if not stat.S_ISREG(temporary_stat.st_mode):
                raise ImmutableIOError("immutable temporary target is unsafe")
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("short immutable artifact write")
                written += count
            os.fsync(descriptor)
        except OSError as exc:
            raise ImmutableIOError("cannot write immutable artifact") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if temporary_stat is None:
            raise ImmutableIOError("immutable temporary target is unavailable")
        staged = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if (staged.st_dev, staged.st_ino) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        ):
            raise ImmutableIOError("immutable temporary target changed")
        os.replace(
            temporary,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        replaced = True
        published = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (published.st_dev, published.st_ino) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
        ) or not stat.S_ISREG(published.st_mode):
            raise ImmutableIOError("immutable artifact changed during replacement")
        os.fsync(parent)
        if _read_regular_at(parent, name) != payload:
            raise ImmutableIOError("immutable artifact changed after replacement")
    except OSError as exc:
        raise ImmutableIOError("cannot replace immutable artifact") from exc
    finally:
        if temporary_stat is not None and not replaced:
            _unlink_same_inode(
                parent,
                temporary,
                device=temporary_stat.st_dev,
                inode=temporary_stat.st_ino,
            )
        os.close(parent)


def write_json_once_or_verify(root: Path, path: Path, value: Mapping[str, Any]) -> bool:
    """Create one canonical JSON artifact, or verify the exact existing bytes."""

    return write_bytes_once_or_verify(root, path, canonical_json_bytes(value))


def replace_json_atomically(root: Path, path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace one canonical JSON artifact."""

    replace_bytes_atomically(root, path, canonical_json_bytes(value))
