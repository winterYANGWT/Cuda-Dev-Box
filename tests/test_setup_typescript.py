import subprocess
import tempfile
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parents[1] / "image" / "setup_typescript.sh"

DEV_TOOLS = "npm install -g typescript ts-node yo generator-code eslint @vscode/vsce"
DEEPSEEK_INSTALL = (
    "npm install -g --registry=https://registry.npmjs.org"
    " --allow-scripts=@deepseek-ai/dsh-subprocess-local,node-pty,koffi,protobufjs,@google/genai"
)


class SetupTypescriptTests(unittest.TestCase):
    def run_setup(self, env=None, npm_status=0):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "calls"
            # npm and the /usr/local/bin links are replaced; the real script
            # decides what gets installed and in which order.
            command = '''log="$2"
npm_status="$3"
ln() { :; }
npm() { printf 'npm %s\\n' "$*" >> "$log"; return "$npm_status"; }
source "$1"
'''
            result = subprocess.run(
                ["bash", "-c", command, "test", str(SETUP), str(log), str(npm_status)],
                text=True, capture_output=True, env={"PATH": "/usr/bin:/bin", **(env or {})},
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return result, calls

    def test_defaults_install_latest_ai_clis(self):
        result, calls = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            DEV_TOOLS,
            "npm install -g --allow-scripts=@anthropic-ai/claude-code"
            " @openai/codex@latest @anthropic-ai/claude-code@latest",
            DEEPSEEK_INSTALL + " @deepseek-ai/dsh@latest",
        ])

    def test_requested_versions_are_passed_to_npm(self):
        env = {
            "CODEX_VERSION": "0.155.1", "CLAUDE_CODE_VERSION": "stable",
            "DEEPSEEK_HARNESS_VERSION": "0.2.1-rc.2",
        }
        result, calls = self.run_setup(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            DEV_TOOLS,
            "npm install -g --allow-scripts=@anthropic-ai/claude-code"
            " @openai/codex@0.155.1 @anthropic-ai/claude-code@stable",
            DEEPSEEK_INSTALL + " @deepseek-ai/dsh@0.2.1-rc.2",
        ])

    def test_failed_npm_call_stops_the_script(self):
        result, calls = self.run_setup(npm_status=7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(calls, [DEV_TOOLS])


if __name__ == "__main__":
    unittest.main()
