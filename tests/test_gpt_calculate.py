import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from app.gpt_calculate import _limit_worker, calculate


class CalculateTest(unittest.TestCase):
    def test_arithmetic_and_json_request(self):
        self.assertEqual(calculate('{"code":"result = (2 + 3) * 4 / 2","data":null}'), {"result": 10.0})

    def test_aggregation_and_comprehensions(self):
        query = {
            "code": "values = [row['amount'] for row in data if row['active']]\n"
                    "result = {'total': sum(values), 'average': round(sum(values) / len(values), 2), "
                    "'sorted': sorted(values, reverse=True), 'unique': len({x for x in values})}",
            "data": [{"amount": 8, "active": True}, {"amount": 3, "active": True}, {"amount": 99, "active": False}],
        }
        self.assertEqual(calculate(query)["result"], {"total": 11, "average": 5.5, "sorted": [8, 3], "unique": 2})

    def test_safe_collection_builtins_and_unpacking(self):
        code = "pairs = list(zip(range(3), data))\nresult = {str(i): value for i, value in pairs}"
        self.assertEqual(calculate({"code": code, "data": ["a", "b", "c"]})["result"], {"0": "a", "1": "b", "2": "c"})

    def test_nested_comprehension_and_scopes(self):
        code = "x = 10\nresult = [[x * y for y in range(3)] for x in range(2)] + [[x]]"
        self.assertEqual(calculate({"code": code})["result"], [[0, 0, 0], [0, 1, 2], [10]])

    def test_rejects_unsafe_language_before_starting_process(self):
        snippets = [
            "import os\nresult = 1", "result = data.__class__", "result = getattr(data, 'x')",
            "result = eval('1')", "result = exec('1')", "result = open('secret')",
            "result = __import__('os').environ", "result = globals()", "result = locals()",
            "result = (lambda: 1)()", "def f():\n return 1\nresult = f()",
            "class X: pass\nresult = 1", "while True: pass", "for x in data: pass",
            "with data: pass", "result = (x for x in data)", "result = sum(*data)",
            "result = dict(**data)", "result = dict(_secret=1)", "result = {**data}", "result = (x := 1)",
            "result = f'{data}'", "result = data[0]()", "result = unknown(data)",
            "result = {x for x in range(2) if (yield x)}", "result = print(data)",
        ]
        with patch("app.gpt_calculate.subprocess.Popen") as popen:
            for code in snippets:
                with self.subTest(code=code), self.assertRaises(ValueError):
                    calculate({"code": code, "data": []})
            popen.assert_not_called()

    def test_rejects_reserved_assignments_and_mutation(self):
        for code in ["data = 1\nresult = data", "sum = 1\nresult = sum", "data[0] = 2\nresult = data",
                     "result = [data for data in [1]]", "_x = 1\nresult = _x", "result = [sum for sum in [1]]"]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                calculate({"code": code, "data": [0]})

    def test_rejects_large_work_and_output(self):
        for code in ["result = 2 ** 1000000000", "result = 'a' * 1000000000",
                     "result = [0] * 1000000000", "result = list(range(1000000000))",
                     "result = [x for x in range(50000) for y in range(50000)]",
                     "result = ['a' * 1000 for x in range(1000)]",
                     "result = [1e308 * 1e308]", "result = float('nan')"]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                calculate({"code": code})

    def test_rejects_bad_input_and_non_json_output(self):
        queries = [None, [], "bad json", {"code": "result = 1", "data": float("nan")},
                   {"code": "result = 1", "data": {1: "bad"}}, {"code": 1},
                   {"code": "x = 1"}, {"code": "result = {1, 2}"},
                   {"code": "result = 1", "data": "a" * 1048576},
                   {"code": "result = 1\n" * 2000}]
        for query in queries:
            with self.subTest(query=str(query)[:80]), self.assertRaises(ValueError):
                calculate(query)

    def test_worker_gets_isolated_interpreter_and_no_credentials(self):
        actual_popen = subprocess.Popen
        observed = []

        def start(args, **kwargs):
            observed.append((args, kwargs))
            return actual_popen(args, **kwargs)

        with patch.dict(os.environ, {"TEST_CALCULATE_SECRET": "private"}), patch("app.gpt_calculate.subprocess.Popen", side_effect=start):
            self.assertEqual(calculate({"code": "result = 42"}), {"result": 42})
        args, kwargs = observed[0]
        self.assertEqual(args[1:3], ["-I", "-S"])
        self.assertNotIn("TEST_CALCULATE_SECRET", kwargs["env"])
        self.assertNotIn("PATH", kwargs["env"])

    def test_timeout_kills_and_reaps_worker(self):
        class StalledProcess:
            returncode = None
            _handle = None

            def __init__(self):
                self.calls = 0
                self.killed = False

            def communicate(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("worker", kwargs["timeout"])
                return b"", b""

            def kill(self):
                self.killed = True

        process = StalledProcess()
        with patch("app.gpt_calculate.subprocess.Popen", return_value=process), patch("app.gpt_calculate._limit_worker"):
            with self.assertRaisesRegex(ValueError, "time"):
                calculate({"code": "result = 1"})
        self.assertTrue(process.killed)
        self.assertEqual(process.calls, 2)

    def test_missing_memory_isolation_fails_closed(self):
        process = unittest.mock.Mock()
        process.returncode = None
        with patch("app.gpt_calculate.subprocess.Popen", return_value=process), patch(
                "app.gpt_calculate._limit_worker", side_effect=ValueError("memory isolation unavailable")):
            with self.assertRaisesRegex(ValueError, "memory isolation"):
                calculate({"code": "result = 1"})
        process.kill.assert_called_once()
        process.communicate.assert_called_once_with()

    def test_worker_crash_is_a_safe_error(self):
        process = unittest.mock.Mock()
        process.returncode = -1
        process.communicate.return_value = (b"", None)
        with patch("app.gpt_calculate.subprocess.Popen", return_value=process), patch("app.gpt_calculate._limit_worker"):
            with self.assertRaisesRegex(ValueError, "memory limits"):
                calculate({"code": "result = 1"})

    def test_parent_rejects_oversized_json_before_starting_worker(self):
        with patch("app.gpt_calculate.subprocess.Popen") as popen:
            for data in ["a" * 1048577, {"a" * 600000: "a" * 600000}, [0] * 50001]:
                with self.assertRaises(ValueError):
                    calculate({"code": "result = data", "data": data})
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object memory limit")
    def test_windows_job_rejects_oversized_allocation(self):
        script = "import sys\nsys.stdin.buffer.read(1)\ntry:\n x = bytearray(512 * 1024 * 1024)\nexcept MemoryError:\n sys.exit(23)\n"
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        close_limit = None
        try:
            close_limit = _limit_worker(process)
            process.communicate(b"x", timeout=5)
            self.assertEqual(process.returncode, 23)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            if close_limit:
                close_limit()


if __name__ == "__main__":
    unittest.main()
