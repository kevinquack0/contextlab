"""Build and verify the immutable ContextLab v1 baseline manifest."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MANIFEST_SCHEMA_VERSION = "contextlab.v1.baseline-manifest.v1"
CHECKPOINT_COMMIT = "41ec50247f0d2a6e87675fbdeaf2dcd8d507258f"


@dataclass(frozen=True)
class SnapshotSpec:
    name: str
    tag: str
    tree: str


@dataclass(frozen=True)
class FingerprintSpec:
    name: str
    snapshot: str
    distractor_count: int
    expected: str


SNAPSHOTS = (
    SnapshotSpec(
        name="complete_raw_v1_snapshot",
        tag="contextlab-v1-raw-2026-06",
        tree="4d60148e0412cc126f44e7a75e5a0027f42e365d",
    ),
    SnapshotSpec(
        name="base_corpus",
        tag="contextlab-v1-base-corpus-2026-06",
        tree="67e219995c47c29033bada03b2d7bbb0a8a92c73",
    ),
    SnapshotSpec(
        name="final_2x_corpus",
        tag="contextlab-v1-final-2x-corpus-2026-06",
        tree="3481ce69c5c73a1b85d9ced2e645255996c9c417",
    ),
    SnapshotSpec(
        name="cliff_corpus",
        tag="contextlab-v1-cliff-corpus-2026-06",
        tree="5cbadb61959335cd7057f21c8e064c4335bf0be5",
    ),
)

FINGERPRINTS = (
    FingerprintSpec("base_1x", "base_corpus", 0, "10829b527b5c"),
    FingerprintSpec("final_2x", "final_2x_corpus", 32, "1e14218de1f5"),
    FingerprintSpec("cliff_1x", "cliff_corpus", 0, "10829b527b5c"),
    FingerprintSpec("cliff_2x", "cliff_corpus", 32, "fcd3e22be1f7"),
    FingerprintSpec("cliff_3x", "cliff_corpus", 64, "5129ac82d3bc"),
)


class BaselineError(RuntimeError):
    """Raised when an immutable v1 baseline check fails."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_manifest_path(root: Path | None = None) -> Path:
    base = root or repository_root()
    return base / "results" / "v2" / "baseline" / "v1_manifest.json"


def _git(root: Path, *args: str, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise BaselineError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout


def _resolve_snapshot(root: Path, spec: SnapshotSpec) -> tuple[str, str]:
    tag_object = str(_git(root, "rev-parse", spec.tag, text=True)).strip()
    resolved = str(_git(root, "rev-parse", f"{spec.tag}^{{}}", text=True)).strip()
    object_type = str(_git(root, "cat-file", "-t", resolved, text=True)).strip()
    if object_type != "tree":
        raise BaselineError(f"{spec.tag} resolves to {object_type}, expected tree")
    if resolved != spec.tree:
        raise BaselineError(f"{spec.tag} resolves to {resolved}, expected {spec.tree}")
    return tag_object, resolved


def _tree_entries(root: Path, tree: str) -> list[dict[str, Any]]:
    raw = bytes(_git(root, "ls-tree", "-rz", "-l", tree))
    entries: list[dict[str, Any]] = []
    aggregate = hashlib.sha256()
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id, size_text = header.split()
        if object_type != b"blob":
            raise BaselineError(f"unexpected non-blob entry in recursive tree: {header!r}")
        content = bytes(_git(root, "cat-file", "blob", object_id.decode("ascii")))
        expected_size = int(size_text)
        if len(content) != expected_size:
            raise BaselineError(
                f"blob {object_id.decode('ascii')} is {len(content)} bytes, expected {expected_size}"
            )
        path = raw_path.decode("utf-8", errors="surrogateescape")
        sha256 = hashlib.sha256(content).hexdigest()
        entry = {
            "path": path,
            "mode": mode.decode("ascii"),
            "git_blob_sha1": object_id.decode("ascii"),
            "size_bytes": expected_size,
            "sha256": sha256,
        }
        entries.append(entry)
        aggregate.update(
            json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8", errors="surrogateescape"
            )
        )
        aggregate.update(b"\n")
    return entries + [{"_content_manifest_sha256": aggregate.hexdigest()}]


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Match the frozen v1 parser for corpus fingerprint verification."""

    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + 5 :]
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("[") and value.endswith("]"):
            value = [part.strip().strip('"') for part in value[1:-1].split(",") if part.strip()]
        elif value.lower() == "true":
            value = True
        elif value.lower() == "false":
            value = False
        else:
            try:
                value = int(value)
            except ValueError:
                pass
        meta[key] = value
    return meta, body


def _blob_map(root: Path, tree: str) -> dict[str, bytes]:
    raw = bytes(_git(root, "ls-tree", "-rz", "-r", tree))
    blobs: dict[str, bytes] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        _mode, object_type, object_id = header.split()
        if object_type != b"blob":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        blobs[path] = bytes(_git(root, "cat-file", "blob", object_id.decode("ascii")))
    return blobs


def _corpus_paths(blobs: dict[str, bytes], distractor_count: int) -> list[str]:
    base_prefix = "corpus/" if any(path.startswith("corpus/") for path in blobs) else ""
    base_paths = sorted(
        path
        for path in blobs
        if path.startswith(base_prefix)
        and path.endswith(".md")
        and (base_prefix == "" or path.startswith("corpus/"))
        and "evaluation_only_do_not_index" not in PurePosixPath(path).parts
        and "/distractors/" not in f"/{path}"
    )
    if base_prefix == "":
        base_paths = [path for path in base_paths if path.split("/", 1)[0][:2].isdigit()]
    distractors = sorted(
        path
        for path in blobs
        if path.startswith("distractors/DST-") and path.endswith(".md")
    )[:distractor_count]
    return base_paths + distractors


def corpus_fingerprint(root: Path, tree: str, distractor_count: int) -> tuple[str, int]:
    blobs = _blob_map(root, tree)
    paths = _corpus_paths(blobs, distractor_count)
    records: list[str] = []
    for path in paths:
        text = blobs[path].decode("utf-8")
        meta, body = _parse_frontmatter(text)
        source_id = str(meta.get("source_id") or PurePosixPath(path).name.split("_", 1)[0])
        title = str(meta.get("title") or source_id)
        records.append(f"{source_id}:{title}:{len(body.strip())}")
    payload = "\n".join(records)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12], len(paths)


def build_manifest(root: Path | None = None) -> dict[str, Any]:
    repo = (root or repository_root()).resolve()
    snapshot_rows: list[dict[str, Any]] = []
    specs_by_name = {spec.name: spec for spec in SNAPSHOTS}
    for spec in SNAPSHOTS:
        tag_object, resolved = _resolve_snapshot(repo, spec)
        raw_entries = _tree_entries(repo, resolved)
        digest_row = raw_entries.pop()
        snapshot_rows.append(
            {
                "name": spec.name,
                "tag": spec.tag,
                "tag_object_sha1": tag_object,
                "tree_sha1": resolved,
                "file_count": len(raw_entries),
                "total_bytes": sum(entry["size_bytes"] for entry in raw_entries),
                "content_manifest_sha256": digest_row["_content_manifest_sha256"],
                "files": raw_entries,
            }
        )

    fingerprint_rows: list[dict[str, Any]] = []
    for check in FINGERPRINTS:
        spec = specs_by_name[check.snapshot]
        actual, document_count = corpus_fingerprint(repo, spec.tree, check.distractor_count)
        if actual != check.expected:
            raise BaselineError(
                f"{check.name} fingerprint is {actual}, expected saved value {check.expected}"
            )
        fingerprint_rows.append(
            {
                "name": check.name,
                "snapshot": check.snapshot,
                "distractor_count": check.distractor_count,
                "document_count": document_count,
                "saved_v1_fingerprint": check.expected,
                "verified": True,
            }
        )

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "baseline": "ContextLab v1",
        "checkpoint_commit": CHECKPOINT_COMMIT,
        "hash_algorithm": "SHA-256",
        "deterministic": True,
        "protected_roots": [
            "novalearn_synthetic_corpus/evaluation_only_do_not_index",
            "novalearn_synthetic_corpus/evaluation_only_do_not_index/v2",
        ],
        "fingerprint_checks": fingerprint_rows,
        "snapshots": snapshot_rows,
    }


def write_manifest(manifest: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot load baseline manifest {path}: {exc}") from exc


def verify_manifest(path: Path, root: Path | None = None) -> dict[str, Any]:
    saved = load_manifest(path)
    current = build_manifest(root)
    if saved != current:
        raise BaselineError("saved v1 manifest does not match the durable Git snapshots")
    return current


def snapshot_file_counts(manifest: dict[str, Any]) -> Iterable[tuple[str, int]]:
    for snapshot in manifest["snapshots"]:
        yield snapshot["name"], snapshot["file_count"]
