"""CLI 冒烟：init-db -> submit -> pending -> approve -> search -> trace。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def run_cli(db_path, *args, token=None, expect_ok=True):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    if token:
        env["KB_TOKEN"] = token
    else:
        env.pop("KB_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-m", "experience_registry.cli", "--db", db_path, *args],
        capture_output=True, text=True, env=env, timeout=30,
    )
    if expect_ok and result.returncode != 0:
        raise AssertionError(f"CLI 失败：{args}\n{result.stderr}")
    return result


class CliTests(unittest.TestCase):
    def test_full_chain_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "kb.db")
            run_cli(db_path, "init-db", "--admin-password", "admin123")

            login = run_cli(db_path, "login", "--username", "admin", "--password", "admin123")
            admin_token = json.loads(login.stdout)["token"]
            # 工程师与复核员分属不同账号（职责分离）
            run_cli(db_path, "user-create", "eng1", "--user-password", "eng12345",
                    "--roles", "engineer,viewer", token=admin_token)
            run_cli(db_path, "user-create", "rev1", "--user-password", "rev12345",
                    "--roles", "reviewer,viewer", token=admin_token)
            eng_token = json.loads(run_cli(
                db_path, "login", "--username", "eng1", "--password", "eng12345").stdout)["token"]
            rev_token = json.loads(run_cli(
                db_path, "login", "--username", "rev1", "--password", "rev12345").stdout)["token"]

            submit = run_cli(
                db_path, "submit",
                "--equipment-model", "CA6140",
                "--process-step", "精车",
                "--conditions", "45# 钢轴类件",
                "--evidence", "现场验证 SV-118",
                "--content", "主轴卡死时先查轴承发热再决定是否更换。",
                token=eng_token,
            )
            submitted = json.loads(submit.stdout)
            entry_id = submitted["entry"]["id"]
            task_id = submitted["review_task_id"]

            pending = run_cli(db_path, "pending", token=rev_token)
            self.assertIn(task_id, pending.stdout)

            run_cli(db_path, "approve", task_id, "--rationale", "复验有效", token=rev_token)

            search = run_cli(db_path, "search", "--q", "轴承", token=rev_token)
            self.assertEqual(len(json.loads(search.stdout)), 1)

            trace = run_cli(db_path, "trace", entry_id, token=rev_token)
            self.assertIn("提交条目", trace.stdout)
            self.assertIn("复核通过", trace.stdout)

            explain = run_cli(db_path, "explain", submitted["version"]["id"], token=rev_token)
            self.assertEqual(json.loads(explain.stdout)["adoption"]["rationale"], "复验有效")

    def test_cli_reports_permission_error_with_stable_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "kb.db")
            run_cli(db_path, "init-db")
            login = run_cli(db_path, "login", "--username", "admin", "--password", "admin123")
            admin_token = json.loads(login.stdout)["token"]
            run_cli(db_path, "user-create", "guest1", "--user-password", "guest123",
                    "--roles", "viewer", token=admin_token)
            login = run_cli(db_path, "login", "--username", "guest1", "--password", "guest123")
            guest_token = json.loads(login.stdout)["token"]
            result = run_cli(db_path, "user-create", "someone", "--user-password", "pass123",
                             token=guest_token, expect_ok=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PERMISSION_DENIED", result.stderr)


if __name__ == "__main__":
    unittest.main()
