"""[표 3-5] 시연 장면별 검증.  실행: python -m unittest -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo import Demo  # noqa: E402
from guard import policy  # noqa: E402


class PolicyTest(unittest.TestCase):
    def test_risk_matrix(self):
        self.assertEqual(policy.risk_of(["notes.read"])[0], "low")
        self.assertEqual(policy.risk_of(["notes.read", "notes.write"])[0], "mid")
        self.assertEqual(policy.risk_of(["mail.read"])[0], "mid")
        self.assertEqual(policy.risk_of(["notes.read", "mail.send"])[0], "high")  # 가장 높은 등급

    def test_unknown_scope_is_high(self):
        self.assertEqual(policy.risk_of(["something.unknown"])[0], "high")

    def test_thresholds(self):
        self.assertEqual(policy.inactivity_threshold("mid", None), 90)
        self.assertEqual(policy.inactivity_threshold("low", None), 180)
        self.assertEqual(policy.inactivity_threshold("high", 120), 240)  # 예약 주기 × 2


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = Demo(quiet=True)
        cls.out = cls.demo.run()

    @classmethod
    def tearDownClass(cls):
        cls.demo.close()

    def test_1_risk_on_registration(self):
        self.assertEqual(self.out["risk"], {"notes": "mid", "crm": "high"})

    def test_2_pause_keeps_config(self):
        self.assertEqual(self.out["notes_paused_day90"], "paused")
        self.assertTrue(self.out["notes_config_kept"])

    def test_3_inline_reconfirm(self):
        self.assertTrue(self.out["card_blocked"])
        self.assertEqual(self.out["keep_status"], 303)
        self.assertTrue(self.out["retry_ok"])
        self.assertEqual(self.out["nonce_reuse_status"], 403)

    def test_4_scheduled_and_autonomous(self):
        self.assertEqual(self.out["crm_pause_reason"], "absolute_cap")
        self.assertEqual(self.out["sched_alerts"], 1)            # 알림은 1회만
        self.assertEqual(self.out["other_alerts_after_pause"], 0)  # 자율 호출은 알림 없음

    def test_5_agent_cannot_reconfirm(self):
        self.assertTrue(self.out["agent_denied"])
        self.assertGreaterEqual(self.out["denied_events"], 3)
        self.assertTrue(self.out["crm_still_paused"])

    def test_6_release_and_cascade_revoke(self):
        self.assertEqual(self.out["crm_state"], "released")
        self.assertTrue(self.out["crm_config_deleted"])
        self.assertTrue(self.out["revoked"])
        self.assertTrue(self.out["vault_cleared"])
        self.assertFalse(self.out["introspect_active"])  # 발급기관에서 토큰 무효 확인
        self.assertTrue(self.out["released_blocked"])
        self.assertTrue(self.out["crm_tools_hidden"])


if __name__ == "__main__":
    unittest.main()
