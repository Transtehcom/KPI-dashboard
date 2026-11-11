import pathlib
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser navigation regressions")
class NavigationTest(unittest.TestCase):
    def run_script(self, name):
        root=pathlib.Path(__file__).resolve().parents[1]
        result=subprocess.run([shutil.which("node"),str(root/"tests"/name)],cwd=root,
                              capture_output=True,text=True,encoding="utf-8",timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_history_routes_and_admin_workload(self):
        self.run_script("test_navigation.js")

    def test_funnel_direction_selection(self):
        self.run_script("test_funnel_directions.js")

    def test_late_responses_cannot_replace_the_active_view(self):
        self.run_script("test_navigation_races.js")

    def test_customer_delivery_disclosure_and_source_date(self):
        self.run_script("test_supply_delivery.js")

    def test_automatic_sync_status_does_not_stay_stale(self):
        self.run_script("test_sync_status.js")

    def test_monthly_supply_assessment(self):
        self.run_script("test_monthly_assessment.js")

    def test_tender_followup_breakdown_and_manual_counter(self):
        self.run_script("test_tender_followups.js")

    def test_call_feedback_forms(self):
        self.run_script("test_call_feedback.js")

    def test_call_processing_jobs(self):
        self.run_script("test_call_jobs.js")

    def test_daily_call_auto_controls(self):
        self.run_script("test_call_auto.js")

    def test_call_repeat_choice(self):
        self.run_script("test_call_reprocess.js")

    def test_interface_motion(self):
        self.run_script("test_interface_motion.js")

    def test_staff_directory(self):
        self.run_script("test_staff_directory.js")

    def test_gpt_chat_rendering(self):
        self.run_script("test_gpt.js")

    def test_network_failure_recovery(self):
        self.run_script("test_api_recovery.js")
