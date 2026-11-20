import sqlite3
import unittest

from app import bot_model_menu as menu


class ModelMenuTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE bot_model_preferences(owner TEXT PRIMARY KEY, model, effort, fast)")
        menu.ensure_schema(self.db)
        self.selection = {"model": "gpt-6.1-terra", "effort": "medium", "fast": False}

    def start(self, admin=False):
        return menu.begin(self.db, "user", "tg:1:2", admin, self.selection)

    def press(self, value, admin=False):
        return menu.choose(self.db, "user", "tg:1:2", admin, value)

    def saved(self):
        return self.db.execute("SELECT model,effort,fast FROM bot_model_preferences WHERE owner='user'").fetchone()

    def test_staff_finishes_after_reasoning(self):
        models = dict(self.start()["buttons"])
        self.assertEqual(set(models), {"GPT-6.1 Terra", "GPT-6.2 Sol"})
        levels = dict(self.press(models["GPT-6.2 Sol"])["buttons"])
        self.assertEqual(set(levels), {"Низкий", "Средний", "Высокий"})
        self.assertIsNone(self.saved())
        final = self.press(levels["Высокий"])
        self.assertFalse(final["buttons"])
        self.assertEqual(self.saved(), ("gpt-6.2-sol", "high", 0))
        self.assertTrue(self.press(levels["Средний"])["stale"])

    def test_admin_finishes_after_speed(self):
        models = dict(self.start(True)["buttons"])
        levels = dict(self.press(models["GPT-6.2 Astra"], True)["buttons"])
        speeds = dict(self.press(levels["Ультра"], True)["buttons"])
        self.assertIsNone(self.saved())
        self.assertEqual(set(speeds), {"Обычная", "Fast"})
        self.assertTrue(self.press(speeds["Fast"])["stale"])
        self.press(speeds["Fast"], True)
        self.assertEqual(self.saved(), ("gpt-6.2-astra", "ultra", 1))

    def test_old_menu_wrong_user_other_chat_and_forged_choice(self):
        old = dict(self.start()["buttons"])["GPT-6.1 Terra"]
        current = dict(self.start()["buttons"])["GPT-6.1 Terra"]
        self.assertTrue(self.press(old)["stale"])
        self.assertTrue(menu.choose(self.db, "other", "tg:1:2", True, current)["stale"])
        self.assertTrue(menu.choose(self.db, "user", "b24:1:2", True, current)["stale"])
        self.assertTrue(self.press(current.replace("gpt-6.1-terra", "gpt-6.2-astra"))["stale"])
        self.assertLessEqual(len(current.encode()), 64)
        self.assertEqual(len(self.press(current)["buttons"]), 4)
        self.assertIsNone(self.saved())
