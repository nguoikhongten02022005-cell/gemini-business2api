import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CliRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.original_cwd = os.getcwd()
        os.chdir(self.workspace)
        self.env_patch = patch.dict(os.environ, {"AGENT_WORKDIR": str(self.workspace)})
        self.env_patch.start()
        import cli.runtime as runtime
        self.runtime = importlib.reload(runtime)

    def tearDown(self):
        self.env_patch.stop()
        os.chdir(self.original_cwd)
        self.temp_dir.cleanup()

    def test_resolve_accepts_path_inside_workspace(self):
        resolved = self.runtime.resolve("nested/file.txt")
        self.assertTrue(str(resolved).startswith(str(self.workspace)))

    def test_resolve_rejects_path_outside_workspace(self):
        outside = self.workspace.parent / "outside.txt"
        with self.assertRaises(ValueError):
            self.runtime.resolve(str(outside))

    def test_read_file_reads_text_file(self):
        path = self.workspace / "sample.txt"
        path.write_text("hello\nworld", encoding="utf-8")
        runtime = self.runtime.ToolRuntime(mode="read-only")
        result = runtime.read_file("sample.txt")
        self.assertIn("FILE sample.txt", result)
        self.assertIn("1 | hello", result)

    def test_read_file_rejects_binary_file(self):
        path = self.workspace / "binary.bin"
        path.write_bytes(b"\x00\x01\x02")
        with self.assertRaises(ValueError):
            self.runtime.ToolRuntime(mode="read-only").read_file("binary.bin")

    def test_read_file_rejects_large_file(self):
        path = self.workspace / "large.txt"
        path.write_text("a" * 20, encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runtime.read_text(path, max_size=10)

    def test_write_file_creates_and_overwrites(self):
        runtime = self.runtime.ToolRuntime(mode="full-auto")
        created = runtime.write_file("notes.txt", "first")
        self.assertIn("created notes.txt", created)
        updated = runtime.write_file("notes.txt", "second")
        self.assertIn("updated notes.txt", updated)
        self.assertEqual((self.workspace / "notes.txt").read_text(encoding="utf-8"), "second")

    def test_edit_file_replaces_single_match(self):
        path = self.workspace / "edit.txt"
        path.write_text("alpha beta", encoding="utf-8")
        runtime = self.runtime.ToolRuntime(mode="full-auto")
        result = runtime.edit_file("edit.txt", "alpha", "omega")
        self.assertIn("OK: edited edit.txt", result)
        self.assertEqual(path.read_text(encoding="utf-8"), "omega beta")

    def test_edit_file_fails_when_not_found(self):
        path = self.workspace / "edit.txt"
        path.write_text("alpha beta", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runtime.ToolRuntime(mode="full-auto").edit_file("edit.txt", "missing", "omega")

    def test_edit_file_fails_when_multiple_matches(self):
        path = self.workspace / "edit.txt"
        path.write_text("alpha alpha", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runtime.ToolRuntime(mode="full-auto").edit_file("edit.txt", "alpha", "omega")

    def test_multi_edit_applies_edits_in_order(self):
        path = self.workspace / "multi.txt"
        path.write_text("alpha beta gamma", encoding="utf-8")
        runtime = self.runtime.ToolRuntime(mode="full-auto")
        result = runtime.multi_edit("multi.txt", [
            {"old_str": "alpha", "new_str": "omega"},
            {"old_str": "gamma", "new_str": "delta"},
        ])
        self.assertIn("applied 2 edits", result)
        self.assertEqual(path.read_text(encoding="utf-8"), "omega beta delta")

    def test_multi_edit_fails_when_one_edit_does_not_match(self):
        path = self.workspace / "multi.txt"
        path.write_text("alpha beta gamma", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runtime.ToolRuntime(mode="full-auto").multi_edit("multi.txt", [
                {"old_str": "alpha", "new_str": "omega"},
                {"old_str": "missing", "new_str": "delta"},
            ])

    def test_list_dir_formats_entries(self):
        (self.workspace / "dir_a").mkdir()
        (self.workspace / "file_a.txt").write_text("hello", encoding="utf-8")
        result = self.runtime.ToolRuntime(mode="read-only").list_dir(".")
        self.assertIn("DIR  dir_a/", result)
        self.assertIn("FILE file_a.txt", result)

    def test_grep_search_finds_literal_text_and_respects_file_pattern(self):
        (self.workspace / "a.py").write_text("needle here", encoding="utf-8")
        (self.workspace / "b.txt").write_text("needle there", encoding="utf-8")
        runtime = self.runtime.ToolRuntime(mode="read-only")
        result = runtime.grep_search("needle", ".", "*.py")
        self.assertIn("FILE a.py", result)
        self.assertNotIn("b.txt", result)

    def test_validate_command_allows_safe_commands(self):
        self.assertEqual(self.runtime.validate_command("pwd"), ["pwd"])
        self.assertEqual(self.runtime.validate_command("ls ."), ["ls", "."])
        self.assertEqual(self.runtime.validate_command("git status"), ["git", "status"])
        self.assertEqual(self.runtime.validate_command("python -m py_compile x.py"), ["python", "-m", "py_compile", "x.py"])

    def test_validate_command_blocks_shell_metacharacters(self):
        with self.assertRaises(ValueError):
            self.runtime.validate_command("pwd && ls")

    def test_validate_command_blocks_dangerous_git_subcommand(self):
        with self.assertRaises(ValueError):
            self.runtime.validate_command("git reset --hard")

    def test_read_only_mode_blocks_mutating_tools(self):
        runtime = self.runtime.ToolRuntime(mode="read-only")
        with self.assertRaises(ValueError):
            runtime.execute("write_file", {"path": "x.txt", "content": "hi"})

    def test_ask_for_approval_prompts_for_mutating_tool(self):
        prompts = []
        def fake_input(prompt: str) -> str:
            prompts.append(prompt)
            return "y"
        runtime = self.runtime.ToolRuntime(mode="ask-for-approval", input_func=fake_input)
        runtime.execute("write_file", {"path": "x.txt", "content": "hi"})
        self.assertEqual(len(prompts), 1)
        self.assertTrue((self.workspace / "x.txt").exists())

    def test_full_auto_does_not_prompt(self):
        prompts = []
        runtime = self.runtime.ToolRuntime(mode="full-auto", input_func=lambda prompt: prompts.append(prompt) or "n")
        runtime.execute("write_file", {"path": "x.txt", "content": "hi"})
        self.assertEqual(prompts, [])
        self.assertTrue((self.workspace / "x.txt").exists())

    def test_save_and_load_transcript(self):
        payload = {"run_id": "run_1", "steps": [1, 2, 3]}
        saved_path = self.runtime.save_transcript("run_1", payload)
        self.assertTrue(saved_path.exists())
        loaded = self.runtime.load_transcript("run_1")
        self.assertEqual(loaded, payload)


if __name__ == "__main__":
    unittest.main()
