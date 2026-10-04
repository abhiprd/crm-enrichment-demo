import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("precommit_scan", Path(__file__).resolve().parent.parent / "scripts" / "precommit_scan.py")
scan = importlib.util.module_from_spec(spec)
sys.modules["precommit_scan"] = scan
spec.loader.exec_module(scan)

# fixtures are assembled at runtime so no token-shaped literal sits in the source
SECRET = "abcd1234" + "efgh5678"
TOKEN = "xox" + "b-1234567890-abcdefghij"
APIKEY = "sk" + "-" + "a" * 24
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"
USERPATH = "/Us" + "ers/someone/project/x"
PERSONAL = "some" + "one@gmail" + ".com"


def test_blocks_private_files_but_not_the_example_env():
    got = scan.forbidden_paths([".env", ".env.example", "data/keys/d001.json", "data/audit/d001.json", "data/deals.json",
                                "results/v0_noise_runs.json", "results/handcheck/d001.md", "data/transcripts/d001.md",
                                "results/v0_noise.json", "crm/db.py"])
    assert got == [".env", "data/keys/d001.json", "data/audit/d001.json", "data/deals.json",
                   "results/v0_noise_runs.json", "results/handcheck/d001.md"]


def test_flags_secret_shapes_env_values_emails_and_local_paths_without_echoing_them():
    assert "value copied from .env" in scan.scan_line(f"token = '{SECRET}'", [SECRET])
    assert "API or bot token" in scan.scan_line(f"x = '{TOKEN}'", [])
    assert "API or bot token" in scan.scan_line(APIKEY, [])
    assert "private key" in scan.scan_line(PEM, [])
    assert "local user path" in scan.scan_line(f"open('{USERPATH}')", [])
    assert "personal email address" in scan.scan_line(f"contact me at {PERSONAL}", [])
    assert scan.scan_line("ana@northwind.example and noreply@anthropic.com and 1+2@users.noreply.github.com", []) == []
    assert scan.scan_line("an ordinary line of code, MODEL=gpt-6-luna", [SECRET]) == []


def test_only_added_lines_are_scanned_with_file_and_line_numbers():
    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n keep\n-old " + PERSONAL + "\n"
            "+new line\n+key = '" + TOKEN + "'\n")
    assert scan.parse_added_lines(diff) == [("a.py", 2, "new line"), ("a.py", 3, f"key = '{TOKEN}'")]
    out = scan.findings(["a.py"], diff, [])
    assert out == ["a.py:3: API or bot token"]  # the removed line's email is not reported; no value is echoed


def test_env_secrets_reads_only_credential_like_values(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"# c\nOPENAI_API_KEY={SECRET}\nOPENAI_EXTRACTOR_MODEL=gpt-6-luna\nSLACK_BOT_TOKEN=short\nHUBSPOT_SERVICE_KEY={SECRET}2 # x\n")
    assert scan.env_secrets(f) == [SECRET, SECRET + "2"]
    assert scan.env_secrets(tmp_path / "missing") == []
