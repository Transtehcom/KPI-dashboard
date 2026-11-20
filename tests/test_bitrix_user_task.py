import datetime as dt
import sqlite3
import unittest
from unittest import mock

from app import bitrix_app as bx


class UserTaskTest(unittest.TestCase):
    def setUp(self):
        self.portal = "https://portal.example.test"
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        self.app = bx.BitrixApp(connection, {
            "bitrix24": {"webhook": self.portal + "/rest/1/webhook/"},
            "bitrix_app": {"client_id": "local.test"},
        })
        self.app._installed = mock.Mock(return_value={
            "client_id": "local.test", "member_id": "portal", "user_id": 7, "portal": self.portal,
        })
        self.app._access_token = mock.Mock(return_value="installer")
        self.future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        self.app._note_sessions[8] = ("employee", self.future, "portal")
        self.fields = {"TITLE": "Проверка", "RESPONSIBLE_ID": 19}
        patcher = mock.patch.object(bx, "_request")
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    def test_verified_user_and_all_task_roles(self):
        fields = dict(self.fields, ACCOMPLICES=[10, 11], AUDITORS=[12, 13],
                      DESCRIPTION="Проверить остатки", DEADLINE="2026-11-20T12:00:00+05:00", CREATED_BY=7)
        self.request.side_effect = [{"result": {"ID": "8"}}, {"result": {"task": {"id": "45"}}}]
        self.assertEqual(self.app.create_user_task(8, fields), 45)
        profile, add = self.request.call_args_list
        self.assertEqual(profile.args, (self.portal + "/rest/profile.json", {"auth": "employee"}))
        self.assertEqual(add.args, (self.portal + "/rest/tasks.task.add.json",
                                    {"auth": "employee", "fields": dict(fields, CREATED_BY=8)}))
        self.app._access_token.assert_not_called()

    def test_installer_can_create_as_self(self):
        self.request.side_effect = [{"result": {"ID": 7}}, {"result": {"task": {"id": 9}}}]
        self.assertEqual(self.app.create_user_task(7, self.fields), 9)
        self.assertEqual(self.request.call_args.args[1]["auth"], "installer")

    def test_task_read_cannot_use_installer_rights(self):
        self.request.side_effect = [{"result": {"ID": 8}}, bx._Denied()]
        with self.assertRaisesRegex(ValueError, "не предоставил"):
            self.app.read_for_user(8, "tasks.task.get", {"taskId": 45})
        self.assertEqual(self.request.call_args.args[1], {"auth": "employee", "taskId": 45})
        self.app._access_token.assert_not_called()

    def test_missing_expired_or_foreign_employee_session_cannot_borrow_installer(self):
        for session in (None, ("employee", self.future - dt.timedelta(days=1), "portal"),
                        ("employee", self.future, "another")):
            with self.subTest(session=session):
                self.app._note_sessions[8] = session
                with self.assertRaisesRegex(bx.BitrixAppError, "Откройте приложение"):
                    self.app.create_user_task(8, self.fields)
                self.request.assert_not_called()
                self.app._access_token.assert_not_called()

    def test_profile_mismatch_blocks_task(self):
        self.request.return_value = {"result": {"ID": 7}}
        with self.assertRaises(bx.BitrixAppError) as caught:
            self.app.create_user_task(8, self.fields)
        self.assertFalse(caught.exception.ambiguous)
        self.assertEqual(self.request.call_count, 1)

    def test_ambiguous_task_result_is_not_retried(self):
        for outcome in (bx._Transport(), {"result": {"task": {"id": 0}}}, {"result": {}}):
            with self.subTest(outcome=outcome):
                self.request.reset_mock()
                self.request.side_effect = [{"result": {"ID": 8}}, outcome]
                with self.assertRaises(bx.BitrixAppError) as caught:
                    self.app.create_user_task(8, self.fields)
                self.assertTrue(caught.exception.ambiguous)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(self.request.call_count, 2)

    def test_task_scope_and_permission_errors_exclude_remote_details(self):
        for denied, keyword in ((bx._InsufficientScope(), "task"), (bx._Denied("private"), "прав")):
            with self.subTest(denied=denied):
                self.request.side_effect = [{"result": {"ID": 8}}, denied]
                with self.assertRaisesRegex(bx.BitrixAppError, keyword) as caught:
                    self.app.create_user_task(8, self.fields)
                self.assertFalse(caught.exception.ambiguous)
                self.assertNotIn("private", str(caught.exception))
