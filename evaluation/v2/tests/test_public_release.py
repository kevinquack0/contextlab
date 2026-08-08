from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import public_release


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _semantic_artifact(body: dict[str, object]) -> dict[str, object]:
    value = copy.deepcopy(body)
    value["artifact_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return value


class PublicReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "vault"
        self.root.mkdir()
        (self.root / "README.md").write_text("# ContextLab\n", encoding="utf-8")
        self.legacy_sha = "e" * 64
        self.legacy_path = (
            f"viewer/public/artifacts/{self.legacy_sha}/public_component_lab.json"
        )

        g2 = _semantic_artifact(
            {
                "schema_version": "fixture.g2.v1",
                "cell_count": 1,
                "traces": [{"score": 0.5}],
            }
        )
        self.g2_path = "results/v2/retrieval/public_component_lab.json"
        self.g2_bytes = _json_bytes(g2)
        self._write(self.g2_path, self.g2_bytes)

        g4 = _semantic_artifact(
            {
                "schema_version": "fixture.g4.v1",
                "derived_evidence": {"score": 1},
                "public_artifacts": [
                    {
                        "publicPath": self.legacy_path,
                        "sourcePath": self.legacy_path,
                        "sourceSha256": self.legacy_sha,
                        "staticUrl": (
                            f"./artifacts/{self.legacy_sha}/public_component_lab.json"
                        ),
                    }
                ],
            }
        )
        self.g4_path = "results/v2/viewer/g4_export_manifest.json"
        self.g4_bytes = _json_bytes(g4)
        self._write(self.g4_path, self.g4_bytes)

        self.g2_ref = {
            "kind": "report",
            "label": "legacy G2",
            "mediaType": "application/json",
            "path": self.legacy_path,
            "sha256": self.legacy_sha,
            "staticUrl": (f"./artifacts/{self.legacy_sha}/public_component_lab.json"),
        }
        g4_sha = hashlib.sha256(self.g4_bytes).hexdigest()
        self.g4_ref = {
            "kind": "export-manifest",
            "label": "legacy G4",
            "mediaType": "application/json",
            "path": self.g4_path,
            "sha256": g4_sha,
            "staticUrl": f"./artifacts/{g4_sha}/g4_export_manifest.json",
        }
        self.viewer = {
            "schemaVersion": "fixture.viewer.v1",
            "g2Metric": {
                "value": 0.5,
                "unit": "ratio",
                "display": "0.5",
                "provenance": {
                    "artifact": self.g2_ref,
                    "jsonPointer": "/traces/0/score",
                    "runIds": ["fixture-g2"],
                },
            },
            "g4Metric": {
                "value": 1,
                "unit": "count",
                "display": "1",
                "provenance": {
                    "artifact": self.g4_ref,
                    "jsonPointer": "/derived_evidence/score",
                    "runIds": ["fixture-g4"],
                },
            },
            "showcase": {"retrievalWin": {"artifact": self.g2_ref}},
        }
        self.viewer_path = "viewer/public/contextlab-viewer.v1.json"
        self.viewer_bytes = _json_bytes(self.viewer)
        self._write(self.viewer_path, self.viewer_bytes)
        self.config = self._config()
        self._save_config()

    def _write(self, relative: str, data: bytes) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _config(self) -> dict[str, object]:
        g2 = json.loads(self.g2_bytes)
        return {
            "schema_version": public_release.CONFIG_SCHEMA,
            "release_id": "fixture-v1",
            "limits": {
                "max_file_bytes_exclusive": 10_000_000,
                "max_total_bytes_exclusive": 100_000_000,
            },
            "files": [
                {"path": "README.md", "required": True},
                {"path": "public-release.json", "required": True},
            ],
            "trees": [],
            "story_metric_registry": {
                "path": "viewer/src/story/evidence.json",
                "required": False,
            },
            "compact_viewer": {
                "source_export_path": self.viewer_path,
                "source_export_sha256": hashlib.sha256(self.viewer_bytes).hexdigest(),
                "g2": {
                    "canonical_path": self.g2_path,
                    "canonical_file_sha256": hashlib.sha256(self.g2_bytes).hexdigest(),
                    "canonical_artifact_sha256": g2["artifact_sha256"],
                    "legacy_public_path": self.legacy_path,
                    "legacy_public_sha256": self.legacy_sha,
                    "additional_canonical_pointers": ["/cell_count"],
                },
                "g4_manifest": {
                    "canonical_path": self.g4_path,
                    "canonical_file_sha256": hashlib.sha256(self.g4_bytes).hexdigest(),
                },
            },
        }

    def _save_config(self) -> None:
        (self.root / "public-release.json").write_bytes(_json_bytes(self.config))

    def _export(self) -> Path:
        destination = Path(self.temporary.name) / "public"
        planned, manifest = public_release.build_release_plan(self.root)
        public_release._write_release(destination, planned, manifest)
        return destination

    def test_manifest_and_projections_are_deterministic(self) -> None:
        first, first_manifest = public_release.build_release_plan(self.root)
        second, second_manifest = public_release.build_release_plan(self.root)
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(
            {path: record.data for path, record in first.items()},
            {path: record.data for path, record in second.items()},
        )
        exported = self._export()
        summary = public_release.verify_release(exported)
        self.assertEqual(summary["status"], "passed")
        self.assertLess(summary["largest_file_bytes"], 10_000_000)
        rewritten = (exported / self.viewer_path).read_text(encoding="utf-8")
        self.assertNotIn(self.legacy_path, rewritten)
        projections = [
            path
            for path in first
            if path.endswith("g2-component-lab.portfolio.v1.json")
        ]
        self.assertEqual(len(projections), 1)
        projected = json.loads(first[projections[0]].data)
        self.assertEqual(
            projected["source"]["file_sha256"],
            hashlib.sha256(self.g2_bytes).hexdigest(),
        )
        self.assertEqual(
            projected["pointer_map"]["/traces/0/score"],
            "/traces/0/score",
        )

    def test_manifest_detects_tampering_and_unlisted_files(self) -> None:
        exported = self._export()
        (exported / "README.md").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(public_release.PublicReleaseError, "differs"):
            public_release.verify_release(exported)

        exported = Path(self.temporary.name) / "public-extra"
        planned, manifest = public_release.build_release_plan(self.root)
        public_release._write_release(exported, planned, manifest)
        (exported / "unlisted.txt").write_text("extra\n", encoding="utf-8")
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "file set differs"
        ):
            public_release.verify_release(exported)

    def test_rejects_paths_symlinks_private_locations_and_secrets(self) -> None:
        self.config["files"].append({"path": "../escape.txt", "required": True})
        self._save_config()
        with self.assertRaisesRegex(public_release.PublicReleaseError, "normalized"):
            public_release.build_release_plan(self.root)

        self.config = self._config()
        (self.root / "unsafe.txt").write_text(
            "private: /" + "Users/example/private/report.md\n", encoding="utf-8"
        )
        self.config["files"].append({"path": "unsafe.txt", "required": True})
        self._save_config()
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "private absolute"
        ):
            public_release.build_release_plan(self.root)

        (self.root / "unsafe.txt").write_text(
            "token=" + "ghp_" + ("a" * 40) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(public_release.PublicReleaseError, "GitHub token"):
            public_release.build_release_plan(self.root)

        (self.root / "unsafe.txt").unlink()
        (self.root / "target.txt").write_text("target\n", encoding="utf-8")
        (self.root / "unsafe.txt").symlink_to(self.root / "target.txt")
        with self.assertRaisesRegex(public_release.PublicReleaseError, "symlink"):
            public_release.build_release_plan(self.root)

    def test_rejects_protected_fields_and_size_limits(self) -> None:
        self._write("unsafe.json", b'{"expected_answer":"private truth"}\n')
        self.config["files"].append({"path": "unsafe.json", "required": True})
        self._save_config()
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "protected field"
        ):
            public_release.build_release_plan(self.root)

        self.config = self._config()
        self.config["limits"]["max_file_bytes_exclusive"] = len(
            (self.root / "README.md").read_bytes()
        )
        self._save_config()
        with self.assertRaisesRegex(public_release.PublicReleaseError, "limit is less"):
            public_release.build_release_plan(self.root)

    def test_rejects_extended_private_protected_and_secret_patterns(self) -> None:
        cases = [
            (
                "unsafe-private.txt",
                ("private path: /" + "private/var/folders/secret.txt\n").encode(),
                "private absolute path",
            ),
            (
                "unsafe-home.txt",
                ("home path: /" + "home/example/private.txt\n").encode(),
                "private absolute path",
            ),
            (
                "unsafe-field.json",
                b'{"goldLabel":"sealed truth"}\n',
                "protected field",
            ),
            (
                "unsafe-protected.md",
                ("Gold " + "answer: sealed truth\n").encode(),
                "protected answer material",
            ),
            (
                "unsafe-slack.txt",
                ("token=xoxb-" + ("1" * 32) + "\n").encode(),
                "Slack token",
            ),
            (
                "unsafe-vercel.txt",
                ("token=vercel_" + ("a" * 32) + "\n").encode(),
                "Vercel token",
            ),
        ]
        for relative, data, expected_error in cases:
            with self.subTest(relative=relative):
                self.config = self._config()
                self._write(relative, data)
                self.config["files"].append({"path": relative, "required": True})
                self._save_config()
                with self.assertRaisesRegex(
                    public_release.PublicReleaseError, expected_error
                ):
                    public_release.build_release_plan(self.root)

    def test_rejects_stale_metric_value_pointer_and_hash(self) -> None:
        wrong_value = copy.deepcopy(self.viewer)
        wrong_value["g2Metric"]["value"] = 0.6
        self.viewer_bytes = _json_bytes(wrong_value)
        self._write(self.viewer_path, self.viewer_bytes)
        self.config = self._config()
        self._save_config()
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "metric value differs"
        ):
            public_release.build_release_plan(self.root)

        wrong_pointer = copy.deepcopy(self.viewer)
        wrong_pointer["g2Metric"]["provenance"]["jsonPointer"] = "/traces/0/missing"
        self.viewer_bytes = _json_bytes(wrong_pointer)
        self._write(self.viewer_path, self.viewer_bytes)
        self.config = self._config()
        self._save_config()
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "pointer does not exist"
        ):
            public_release.build_release_plan(self.root)

        self.viewer_bytes = _json_bytes(self.viewer)
        self._write(self.viewer_path, self.viewer_bytes)
        self.config = self._config()
        self.config["compact_viewer"]["source_export_sha256"] = "0" * 64
        self._save_config()
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "file hash changed"
        ):
            public_release.build_release_plan(self.root)

    def test_rejects_partial_artifact_identity_matches(self) -> None:
        for label, replacement in (
            ("right path with wrong hash", {"sha256": "f" * 64}),
            ("right hash with wrong path", {"path": "viewer/public/wrong.json"}),
        ):
            with self.subTest(label=label):
                viewer = copy.deepcopy(self.viewer)
                for artifact in (
                    viewer["g2Metric"]["provenance"]["artifact"],
                    viewer["showcase"]["retrievalWin"]["artifact"],
                ):
                    artifact.update(replacement)
                    if "sha256" in replacement:
                        artifact["staticUrl"] = (
                            f"./artifacts/{replacement['sha256']}/public_component_lab.json"
                        )
                self.viewer_bytes = _json_bytes(viewer)
                self._write(self.viewer_path, self.viewer_bytes)
                self.config = self._config()
                self._save_config()
                with self.assertRaisesRegex(
                    public_release.PublicReleaseError,
                    "compact rewrite did not replace all required artifacts",
                ):
                    public_release.build_release_plan(self.root)

    def test_validates_markdown_paths_and_anchors(self) -> None:
        self._write(
            "docs/GUIDE.md",
            b"# Guide\n\n## Local verification\n\nRun the verifier.\n",
        )
        self.config["files"].append({"path": "docs/GUIDE.md", "required": True})
        self._write(
            "README.md",
            b"# ContextLab\n\n[Verify](docs/GUIDE.md#local-verification)\n",
        )
        self._save_config()
        public_release.build_release_plan(self.root)

        self._write(
            "README.md",
            b"# ContextLab\n\n[Verify](docs/GUIDE.md#missing-section)\n",
        )
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "missing anchor"
        ):
            public_release.build_release_plan(self.root)

        self._write("README.md", b"# ContextLab\n\n[Missing](docs/MISSING.md)\n")
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "missing target"
        ):
            public_release.build_release_plan(self.root)

    def test_projects_and_verifies_story_metric_lineage(self) -> None:
        g2 = json.loads(self.g2_bytes)
        story_path = "viewer/src/story/evidence.json"
        story = {
            "schema_version": public_release.STORY_EVIDENCE_SCHEMA,
            "metrics": [
                {
                    "id": "g2.cell-count",
                    "value": 1,
                    "status": "approved",
                    "scope": "fixture",
                    "source_path": self.g2_path,
                    "source_file_sha256": hashlib.sha256(self.g2_bytes).hexdigest(),
                    "source_artifact_sha256": g2["artifact_sha256"],
                    "json_pointer": "/cell_count",
                    "public_url": None,
                }
            ],
        }
        self._write(story_path, _json_bytes(story))
        self.config["story_metric_registry"] = {
            "path": story_path,
            "required": True,
        }
        self._save_config()

        planned, _manifest = public_release.build_release_plan(self.root)
        projected = json.loads(planned[story_path].data)
        metric = projected["metrics"][0]
        self.assertNotEqual(metric["source_path"], self.g2_path)
        self.assertEqual(
            metric["source_file_sha256"],
            planned[metric["source_path"]].sha256,
        )
        self.assertEqual(
            metric["public_url"],
            "./" + metric["source_path"].removeprefix("viewer/public/"),
        )
        public_release._verify_story_registry(projected, planned=planned)

        story["metrics"][0]["source_file_sha256"] = "0" * 64
        self._write(story_path, _json_bytes(story))
        with self.assertRaisesRegex(
            public_release.PublicReleaseError, "artifact hash differs"
        ):
            public_release.build_release_plan(self.root)

    def test_materializes_story_public_artifact(self) -> None:
        evidence = _semantic_artifact(
            {"schema_version": "fixture.decision.v1", "decision": "retain-simple"}
        )
        source_path = "results/v2/gates/decision.json"
        source_bytes = _json_bytes(evidence)
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        public_path = f"viewer/public/artifacts/{source_sha}/decision.json"
        self._write(source_path, source_bytes)
        story_path = "viewer/src/story/evidence.json"
        story = {
            "schema_version": public_release.STORY_EVIDENCE_SCHEMA,
            "metrics": [
                {
                    "id": "decision",
                    "value": "retain-simple",
                    "status": "approved",
                    "scope": "fixture",
                    "source_path": source_path,
                    "source_file_sha256": source_sha,
                    "source_artifact_sha256": evidence["artifact_sha256"],
                    "json_pointer": "/decision",
                    "public_url": f"./artifacts/{source_sha}/decision.json",
                }
            ],
        }
        self._write(story_path, _json_bytes(story))
        self.config["files"].append({"path": source_path, "required": True})
        self.config["story_metric_registry"] = {
            "path": story_path,
            "required": True,
        }
        self._save_config()

        planned, _manifest = public_release.build_release_plan(self.root)
        self.assertEqual(planned[public_path].data, source_bytes)
        self.assertEqual(
            planned[public_path].projection_lineage["kind"],
            "story-evidence-public-copy",
        )
        projected = json.loads(planned[story_path].data)
        public_release._verify_story_registry(projected, planned=planned)

    def test_materializes_linked_artifact_beside_markdown_source(self) -> None:
        target_data = b"# Target\n"
        target_sha = hashlib.sha256(target_data).hexdigest()
        target_static = f"viewer/public/artifacts/{target_sha}/target.md"
        self._write(target_static, target_data)
        target_ref = {
            "kind": "source",
            "label": "Target",
            "mediaType": "text/markdown",
            "path": "docs/target.md",
            "sha256": target_sha,
            "staticUrl": f"./artifacts/{target_sha}/target.md",
        }

        source_data = b"# Source\n\n[Target](docs/target.md)\n"
        source_sha = hashlib.sha256(source_data).hexdigest()
        source_static = f"viewer/public/artifacts/{source_sha}/source.md"
        self._write(source_static, source_data)
        source_ref = {
            "kind": "source",
            "label": "Source",
            "mediaType": "text/markdown",
            "path": "docs/source.md",
            "sha256": source_sha,
            "staticUrl": f"./artifacts/{source_sha}/source.md",
        }
        viewer = copy.deepcopy(self.viewer)
        viewer["documents"] = [source_ref, target_ref]
        self.viewer_bytes = _json_bytes(viewer)
        self._write(self.viewer_path, self.viewer_bytes)
        self.config = self._config()
        self._save_config()

        planned, _manifest = public_release.build_release_plan(self.root)
        linked_copy = f"viewer/public/artifacts/{source_sha}/docs/target.md"
        self.assertEqual(planned[linked_copy].data, target_data)
        self.assertEqual(
            planned[linked_copy].projection_lineage["kind"],
            "artifact-link-support-copy",
        )

    def test_prunes_explicit_private_navigation_artifact(self) -> None:
        roadmap_data = b"# Roadmap\n\n[Private note](private/note.md)\n"
        roadmap_sha = hashlib.sha256(roadmap_data).hexdigest()
        roadmap_static = f"viewer/public/artifacts/{roadmap_sha}/roadmap.md"
        self._write(roadmap_static, roadmap_data)
        roadmap_ref = {
            "kind": "source",
            "label": "Historical roadmap",
            "mediaType": "text/markdown",
            "path": roadmap_static,
            "sha256": roadmap_sha,
            "staticUrl": f"./artifacts/{roadmap_sha}/roadmap.md",
        }
        viewer = copy.deepcopy(self.viewer)
        viewer["sourceArtifacts"] = [roadmap_ref]

        g4 = json.loads(self.g4_bytes)
        g4.pop("artifact_sha256")
        g4["public_artifacts"].append(
            {
                "mediaType": "text/markdown",
                "publicPath": roadmap_static,
                "sourcePath": roadmap_static,
                "sourceSha256": roadmap_sha,
                "staticUrl": f"./artifacts/{roadmap_sha}/roadmap.md",
            }
        )
        self.g4_bytes = _json_bytes(_semantic_artifact(g4))
        self._write(self.g4_path, self.g4_bytes)
        g4_sha = hashlib.sha256(self.g4_bytes).hexdigest()
        viewer["g4Metric"]["provenance"]["artifact"] = {
            **self.g4_ref,
            "sha256": g4_sha,
            "staticUrl": f"./artifacts/{g4_sha}/g4_export_manifest.json",
        }
        self.viewer_bytes = _json_bytes(viewer)
        self._write(self.viewer_path, self.viewer_bytes)
        self.config = self._config()
        self.config["compact_viewer"]["excluded_artifacts"] = [
            {
                "path": roadmap_static,
                "sha256": roadmap_sha,
                "reason": "Private navigation is outside the public allowlist.",
            }
        ]
        self._save_config()

        planned, _manifest = public_release.build_release_plan(self.root)
        self.assertNotIn(roadmap_static, planned)
        public_viewer = planned[self.viewer_path].data.decode("utf-8")
        self.assertNotIn(roadmap_sha, public_viewer)

    def test_replaces_excluded_method_source_without_emptying_group(self) -> None:
        roadmap_data = b"# Historical roadmap\n"
        roadmap_sha = hashlib.sha256(roadmap_data).hexdigest()
        roadmap_static = f"viewer/public/artifacts/{roadmap_sha}/roadmap.md"
        self._write(roadmap_static, roadmap_data)
        roadmap_ref = {
            "kind": "source",
            "label": "Historical roadmap",
            "mediaType": "text/markdown",
            "path": roadmap_static,
            "sha256": roadmap_sha,
            "staticUrl": f"./artifacts/{roadmap_sha}/roadmap.md",
        }

        replacement_data = b"# Public source and agent boundary\n"
        replacement_sha = hashlib.sha256(replacement_data).hexdigest()
        replacement_static = (
            f"viewer/public/artifacts/{replacement_sha}/source-boundary.md"
        )
        self._write(replacement_static, replacement_data)
        replacement_ref = {
            "kind": "source",
            "label": "Public source and agent boundary",
            "mediaType": "text/markdown",
            "path": replacement_static,
            "sha256": replacement_sha,
            "staticUrl": f"./artifacts/{replacement_sha}/source-boundary.md",
        }

        viewer = copy.deepcopy(self.viewer)
        viewer["methods"] = {
            "sourceMap": [
                {
                    "label": "AI-Brain planning map",
                    "description": "Planning material is separate from evidence.",
                    "artifacts": [roadmap_ref],
                }
            ]
        }

        g4 = json.loads(self.g4_bytes)
        g4.pop("artifact_sha256")
        g4["public_artifacts"].append(
            {
                "mediaType": "text/markdown",
                "publicPath": roadmap_static,
                "sourcePath": roadmap_static,
                "sourceSha256": roadmap_sha,
                "staticUrl": f"./artifacts/{roadmap_sha}/roadmap.md",
            }
        )
        self.g4_bytes = _json_bytes(_semantic_artifact(g4))
        self._write(self.g4_path, self.g4_bytes)
        g4_sha = hashlib.sha256(self.g4_bytes).hexdigest()
        viewer["g4Metric"]["provenance"]["artifact"] = {
            **self.g4_ref,
            "sha256": g4_sha,
            "staticUrl": f"./artifacts/{g4_sha}/g4_export_manifest.json",
        }
        self.viewer_bytes = _json_bytes(viewer)
        self._write(self.viewer_path, self.viewer_bytes)
        self.config = self._config()
        self.config["compact_viewer"]["excluded_artifacts"] = [
            {
                "path": roadmap_static,
                "sha256": roadmap_sha,
                "reason": "Historical planning links are outside the public release.",
                "replacement": replacement_ref,
            }
        ]
        self._save_config()

        planned, _manifest = public_release.build_release_plan(self.root)
        public_viewer = json.loads(planned[self.viewer_path].data)
        artifacts = public_viewer["methods"]["sourceMap"][0]["artifacts"]
        self.assertEqual(artifacts, [replacement_ref])
        self.assertIn(replacement_static, planned)
        self.assertNotIn(roadmap_static, planned)


if __name__ == "__main__":
    unittest.main()
