#!/usr/bin/env python3
"""CLI tests for transactional methodology configuration."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/claude-obsidian.py"

sys.path.insert(0, str(ROOT))

import claude_obsidian.cli as cli_module
import claude_obsidian.transaction as transaction_module
from claude_obsidian.transaction import TransactionValidationError


class ModeCliTests(unittest.TestCase):
    def vault(self, parent: Path) -> Path:
        vault = parent / "vault"
        (vault / ".obsidian").mkdir(parents=True)
        (vault / "wiki").mkdir()
        (vault / ".raw").mkdir()
        return vault

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_set_is_dry_run_then_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = self.vault(Path(directory))
            common = (
                "mode",
                "set",
                "para",
                "--vault",
                str(vault),
                "--generated-at",
                "2026-07-11T12:00:00Z",
                "--operation-id",
                "mode-test",
            )
            planned = self.invoke(*common)
            self.assertEqual(0, planned.returncode, planned.stderr)
            plan = json.loads(planned.stdout)
            self.assertEqual("dry-run", plan["status"])
            self.assertFalse((vault / ".vault-meta").exists())

            applied = self.invoke(
                *common,
                "--approved-plan-sha256",
                plan["approved_plan_sha256"],
                "--apply",
            )
            self.assertEqual(0, applied.returncode, applied.stderr)
            result = json.loads(applied.stdout)
            self.assertEqual([".vault-meta/mode.json"], result["changed_paths"])
            config = json.loads(
                (vault / ".vault-meta/mode.json").read_text(encoding="utf-8")
            )
            self.assertEqual("para", config["mode"])
            self.assertEqual("2026-07-11T12:00:00Z", config["configured_at"])

            current = self.invoke("mode", "get", "--vault", str(vault))
            self.assertEqual(0, current.returncode, current.stderr)
            self.assertEqual("para", json.loads(current.stdout)["mode"])

    def test_existing_custom_routes_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = self.vault(Path(directory))
            meta = vault / ".vault-meta"
            meta.mkdir()
            existing = {
                "schema_version": 1,
                "mode": "generic",
                "configured_at": "earlier",
                "config": {
                    "custom": {"folder": "wiki/custom/"},
                    "zettelkasten": {"id_format": "YYYYMMDDHHMMSSffffff"},
                },
            }
            (meta / "mode.json").write_text(json.dumps(existing), encoding="utf-8")
            common = (
                "mode",
                "set",
                "lyt",
                "--vault",
                str(vault),
                "--generated-at",
                "2026-07-11T12:00:00Z",
                "--operation-id",
                "mode-preserve",
            )
            preview = self.invoke(*common)
            self.assertEqual(0, preview.returncode, preview.stderr)
            approval = json.loads(preview.stdout)["approved_plan_sha256"]
            result = self.invoke(
                *common,
                "--approved-plan-sha256",
                approval,
                "--apply",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            updated = json.loads((meta / "mode.json").read_text(encoding="utf-8"))
            self.assertEqual({"folder": "wiki/custom/"}, updated["config"]["custom"])
            self.assertEqual(
                "YYYYMMDDHHMMSSffffff-UUID4HEX",
                updated["config"]["zettelkasten"]["id_format"],
            )

    def test_apply_requires_exact_reviewed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = self.vault(Path(directory))
            common = (
                "mode",
                "set",
                "para",
                "--vault",
                str(vault),
                "--generated-at",
                "2026-07-11T12:00:00Z",
                "--operation-id",
                "mode-binding",
            )
            missing = self.invoke(*common, "--apply")
            self.assertEqual(2, missing.returncode)
            self.assertIn("PLAN_APPROVAL_REQUIRED", missing.stderr)

            preview = json.loads(self.invoke(*common).stdout)
            changed = self.invoke(
                *common,
                "--operation-id",
                "mode-binding-changed",
                "--approved-plan-sha256",
                preview["approved_plan_sha256"],
                "--apply",
            )
            self.assertEqual(2, changed.returncode)
            self.assertIn("PLAN_CHANGED", changed.stderr)
            self.assertFalse((vault / ".vault-meta").exists())

    def test_unsafe_existing_route_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = self.vault(Path(directory))
            meta = vault / ".vault-meta"
            meta.mkdir()
            (meta / "mode.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "generic",
                        "configured_at": "earlier",
                        "config": {
                            "generic": {"entities_folder": "../../outside/"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = self.invoke(
                "mode",
                "set",
                "lyt",
                "--vault",
                str(vault),
                "--generated-at",
                "2026-07-12T00:00:00Z",
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("existing mode configuration is unsafe", result.stderr)
            self.assertFalse((vault / "outside").exists())

    def test_mode_config_symlink_is_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            vault = self.vault(base)
            outside = base / "outside-mode.json"
            outside.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "generic",
                        "secret_sentinel": "EXTERNAL_MODE_SENTINEL",
                        "config": {},
                    }
                ),
                encoding="utf-8",
            )
            meta = vault / ".vault-meta"
            meta.mkdir()
            (meta / "mode.json").symlink_to(outside)
            result = self.invoke("mode", "get", "--vault", str(vault))
            self.assertEqual(2, result.returncode)
            self.assertNotIn("EXTERNAL_MODE_SENTINEL", result.stdout + result.stderr)

    def test_duplicate_mode_authority_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = self.vault(Path(directory))
            meta = vault / ".vault-meta"
            meta.mkdir()
            (meta / "mode.json").write_text(
                '{"schema_version":1,"mode":"generic","mode":"para","config":{}}',
                encoding="utf-8",
            )
            for command in (
                ("mode", "get", "--vault", str(vault)),
                (
                    "mode",
                    "set",
                    "lyt",
                    "--vault",
                    str(vault),
                    "--generated-at",
                    "2026-07-12T00:00:00Z",
                ),
            ):
                result = self.invoke(*command)
                self.assertEqual(2, result.returncode)
                self.assertIn("duplicate JSON object key", result.stderr)


class ModePlanPreparationTests(unittest.TestCase):
    """The plan is prepared once per CLI pass, never speculatively."""

    def vault(self, parent: Path) -> Path:
        vault = parent / "vault"
        (vault / ".obsidian").mkdir(parents=True)
        (vault / "wiki").mkdir()
        (vault / ".raw").mkdir()
        return vault

    def namespace(self, vault: Path, **overrides: object) -> argparse.Namespace:
        fields: dict[str, object] = {
            "vault": str(vault),
            "mode": "para",
            "generated_at": "2026-07-11T12:00:00Z",
            "operation_id": "mode-prepare-count",
            "apply": False,
            "approved_plan_sha256": None,
        }
        fields.update(overrides)
        return argparse.Namespace(**fields)

    def run_counting_preparations(self, args: argparse.Namespace) -> int:
        """Run the command, returning how many times a plan was prepared."""

        calls = 0
        real = transaction_module._prepare_writes

        def counting(*positional: object, **keyword: object) -> object:
            nonlocal calls
            calls += 1
            return real(*positional, **keyword)

        with mock.patch.object(transaction_module, "_prepare_writes", counting):
            with contextlib.redirect_stdout(io.StringIO()):
                cli_module.command_mode_set(args)
        return calls

    def test_dry_run_prepares_the_plan_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = self.vault(Path(directory))
            calls = self.run_counting_preparations(self.namespace(vault))
            self.assertEqual(1, calls)

    def test_apply_prepares_the_plan_exactly_twice(self) -> None:
        # Two preparations are load-bearing and must not be collapsed further:
        # the CLI rejects a stale plan *before* any vault state is created, and
        # apply_bundle re-derives it under the mutation lock, which is the
        # authoritative check. Anything beyond those two is waste.
        with tempfile.TemporaryDirectory() as directory:
            vault = self.vault(Path(directory))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli_module.command_mode_set(self.namespace(vault))
            approval = json.loads(buffer.getvalue())["approved_plan_sha256"]

            calls = self.run_counting_preparations(
                self.namespace(vault, apply=True, approved_plan_sha256=approval)
            )
            self.assertEqual(2, calls)
            self.assertEqual(
                "para", json.loads((vault / ".vault-meta/mode.json").read_text())["mode"]
            )

    def test_apply_still_rejects_a_stale_plan_before_touching_the_vault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = self.vault(Path(directory))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli_module.command_mode_set(self.namespace(vault))
            approval = json.loads(buffer.getvalue())["approved_plan_sha256"]

            stale = self.namespace(
                vault,
                apply=True,
                approved_plan_sha256=approval,
                operation_id="mode-prepare-count-changed",
            )
            with self.assertRaises(TransactionValidationError) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    cli_module.command_mode_set(stale)
            self.assertEqual("PLAN_CHANGED", caught.exception.code)
            self.assertFalse((vault / ".vault-meta/mode.json").exists())


if __name__ == "__main__":
    unittest.main()
