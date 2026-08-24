"""BOSS is a separate product namespace, not a theme sharing Firstmate runtime state."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class ProductIdentityTests(unittest.TestCase):
    def test_runtime_namespace_has_no_firstmate_state_or_command_aliases(self):
        roots = [REPO / "bin", REPO / "bossctl", REPO / ".pi" / "extensions", REPO / "graphs"]
        paths = [path for root in roots for path in root.rglob("*")
                 if path.is_file() and (path.parent == REPO / "bin" or path.suffix in {".py", ".ts", ".yaml"})]
        paths += [REPO / "install.sh", REPO / "AGENTS.md", REPO / "SKILL.md"]
        text = "\n".join(path.read_text(errors="replace") for path in paths)
        for forbidden in ("pi-firstmate", "~/.helm", "HELM_", "from helm", "import helm",
                          "session stop firstmate", "HERDR_SESSION=firstmate"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertIn("~/.boss", text)
        self.assertIn("BOSS_HOME", text)
        self.assertIn('default="boss"', text)
        self.assertIn('return f"boss/', text)
        self.assertNotIn('pi.exec("bossctl"', text)
        self.assertIn("../../bin/bossctl", text)

    def test_public_commands_and_skill_are_only_boss_named(self):
        expected = {"bossctl", "pi-boss", "pi-boss-quit"}
        self.assertEqual({path.name for path in (REPO / "bin").iterdir() if path.is_file()}, expected)
        self.assertRegex((REPO / "SKILL.md").read_text(), r"(?m)^name: boss$")
        install = (REPO / "install.sh").read_text()
        for command in expected:
            self.assertIn(command, install)
        self.assertNotRegex(install, re.compile(r"git\s+-C\s+\"\$target\"\s+pull"))
        self.assertIn("existing $target checkout unchanged", install)

    def test_prompt_role_and_portfolio_command_are_unambiguous(self):
        agents = (REPO / "AGENTS.md").read_text()
        self.assertIn("chief operating officer (COO)", agents)
        self.assertIn("The user is **Boss**", agents)
        self.assertIn("Never call yourself Boss", agents)
        self.assertIn("`/ops` is the canonical portfolio view", agents)
        self.assertNotIn("`/fleet`", agents)


if __name__ == "__main__":
    unittest.main()
