"""Bounded JSON calculations in a fresh, credential-free Python interpreter.

This is an interpreted calculation language. The allowlisted AST is never passed
to eval/exec, and runtime values cannot contain Python objects or callables.
"""

import ast
import json
import math
import os
from pathlib import Path
import subprocess
import sys


MAX_INPUT_BYTES = 1024 * 1024
MAX_CODE_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_AST_NODES = 4096
MAX_DEPTH = 64
MAX_ITEMS = 50000
MAX_STEPS = 250000
MAX_INT_BITS = 4096
WORKER_MEMORY_BYTES = 256 * 1024 * 1024
WORKER_TIMEOUT_SECONDS = 5

_BUILTINS = frozenset({
    "sum", "len", "min", "max", "round", "sorted", "range", "abs", "all",
    "any", "int", "float", "str", "list", "dict", "tuple", "set", "enumerate", "zip",
})
_ALLOWED_NODES = (
    ast.Module, ast.Assign, ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Subscript, ast.Slice,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UnaryOp, ast.UAdd, ast.USub, ast.Not, ast.BoolOp, ast.And, ast.Or,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.IfExp, ast.Call, ast.keyword, ast.ListComp, ast.SetComp, ast.DictComp,
    ast.comprehension,
)


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _target_names(item)]
    raise ValueError("Only plain variable assignment is allowed")


def _validate(code):
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError("Calculation code is missing or too large")
    try:
        tree = ast.parse(code, mode="exec")
    except (SyntaxError, RecursionError, ValueError, MemoryError) as exc:
        raise ValueError("Invalid calculation syntax") from exc
    pending = [(tree, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_DEPTH:
            raise ValueError("Calculation syntax is too complex")
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError("Unsupported calculation syntax")
        if isinstance(node, ast.Name) and "_" in node.id:
            raise ValueError("Underscore identifiers are not allowed")
        if isinstance(node, ast.Constant) and type(node.value) not in (str, int, float, bool, type(None)):
            raise ValueError("Unsupported literal")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _BUILTINS:
                raise ValueError("Only registered calculation functions are allowed")
            if any(keyword.arg is None for keyword in node.keywords):
                raise ValueError("Argument expansion is not allowed")
            if any("_" in keyword.arg for keyword in node.keywords):
                raise ValueError("Underscore identifiers are not allowed")
        if isinstance(node, ast.Dict) and any(key is None for key in node.keys):
            raise ValueError("Dictionary expansion is not allowed")
        if isinstance(node, (ast.Assign, ast.comprehension)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _target_names(target):
                    if name == "data" or name in _BUILTINS or "_" in name:
                        raise ValueError("Reserved variable cannot be assigned")
            if isinstance(node, ast.comprehension) and node.is_async:
                raise ValueError("Async calculations are not allowed")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    if any(not isinstance(statement, ast.Assign) for statement in tree.body):
        raise ValueError("Only assignments are allowed")
    return tree


def _check_json(value, *, output=False):
    """Check structure without invoking user-supplied object conversion methods."""
    pending = [(value, 0)]
    nodes = 0
    text_bytes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_ITEMS or depth > MAX_DEPTH:
            raise ValueError("JSON structure is too complex")
        if type(item) in (list, tuple):
            if not output and type(item) is tuple:
                raise ValueError("Input must contain JSON values")
            if len(item) > MAX_ITEMS:
                raise ValueError("JSON structure is too complex")
            pending.extend((part, depth + 1) for part in item)
        elif type(item) is dict:
            if len(item) > MAX_ITEMS:
                raise ValueError("JSON structure is too complex")
            if any(type(key) is not str for key in item):
                raise ValueError("JSON object keys must be strings")
            pending.extend((key, depth + 1) for key in item)
            pending.extend((part, depth + 1) for part in item.values())
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError("Calculation result must contain JSON values")
        elif type(item) is float and not math.isfinite(item):
            raise ValueError("Numbers must be finite")
        elif type(item) is int and item.bit_length() > MAX_INT_BITS:
            raise ValueError("Integer is too large")
        elif type(item) is str:
            if len(item) > MAX_INPUT_BYTES:
                raise ValueError("JSON text is too large")
            text_bytes += len(item.encode("utf-8"))
            if text_bytes > MAX_INPUT_BYTES:
                raise ValueError("JSON text is too large")


class _Evaluator:
    def __init__(self, data):
        self.variables = {"data": data}
        self.steps = 0

    def _tick(self):
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise ValueError("Calculation work limit exceeded")

    def _guard(self, value):
        pending = [value]
        count = 0
        total_text = 0
        while pending:
            item = pending.pop()
            count += 1
            if count > MAX_ITEMS:
                raise ValueError("Collection is too large")
            if isinstance(item, (list, tuple, set, dict, range)):
                if len(item) > MAX_ITEMS:
                    raise ValueError("Collection is too large")
                if isinstance(item, dict):
                    pending.extend(item.keys())
                    pending.extend(item.values())
                elif not isinstance(item, range):
                    pending.extend(item)
            elif type(item) is int and item.bit_length() > MAX_INT_BITS:
                raise ValueError("Integer is too large")
            elif type(item) is float and not math.isfinite(item):
                raise ValueError("Numbers must be finite")
            elif type(item) is str:
                total_text += len(item)
                if total_text > MAX_INPUT_BYTES:
                    raise ValueError("Text is too large")
        return value

    def _iter(self, value):
        if type(value) not in (list, tuple, set, dict, str, range):
            raise ValueError("Expected a collection")
        if len(value) > MAX_ITEMS:
            raise ValueError("Collection is too large")
        return value

    def _assign(self, target, value, variables):
        if isinstance(target, ast.Name):
            variables[target.id] = value
            return
        values = self._iter(value)
        if len(target.elts) != len(values):
            raise ValueError("Assignment size mismatch")
        for child, item in zip(target.elts, values):
            self._assign(child, item, variables)

    def _binary(self, operator, left, right):
        self._tick()
        if isinstance(operator, ast.Pow):
            if type(left) not in (int, float, bool) or type(right) not in (int, float, bool):
                raise ValueError("Power requires numbers")
            if abs(right) > 10000 or (type(left) is int and right > 0 and left.bit_length() * right > MAX_INT_BITS):
                raise ValueError("Power is too large")
            result = left ** right
            if type(result) is complex:
                raise ValueError("Complex numbers are not supported")
        elif isinstance(operator, ast.Mult):
            for sequence, multiplier in ((left, right), (right, left)):
                if isinstance(sequence, (list, tuple, str)) and isinstance(multiplier, int):
                    limit = MAX_OUTPUT_BYTES if isinstance(sequence, str) else MAX_ITEMS
                    if len(sequence) * max(multiplier, 0) > limit:
                        raise ValueError("Multiplication result is too large")
            result = left * right
        elif isinstance(operator, ast.Add):
            if type(left) is type(right) and isinstance(left, (str, list, tuple)):
                limit = MAX_INPUT_BYTES if isinstance(left, str) else MAX_ITEMS
                if len(left) + len(right) > limit:
                    raise ValueError("Addition result is too large")
            result = left + right
        elif isinstance(operator, ast.Sub):
            result = left - right
        elif isinstance(operator, ast.Div):
            result = left / right
        elif isinstance(operator, ast.FloorDiv):
            result = left // right
        else:
            # String formatting can allocate arbitrary amounts before a size check.
            if isinstance(left, str):
                raise ValueError("String formatting is not supported")
            result = left % right
        return self._guard(result)

    def _call(self, name, args, kwargs):
        self._tick()
        keyword_names = {"sorted": {"reverse"}, "round": {"ndigits"}, "min": {"default"}, "max": {"default"}}
        if name != "dict" and not set(kwargs).issubset(keyword_names.get(name, set())):
            raise ValueError("Unsupported function arguments")
        if name == "range":
            result = range(*args)
            self._iter(result)
        elif name in ("list", "tuple", "set", "dict"):
            if len(args) > 1:
                raise ValueError("Too many collection arguments")
            values = self._iter(args[0]) if args else []
            constructors = {"list": list, "tuple": tuple, "set": set, "dict": dict}
            result = constructors[name](values, **kwargs)
        elif name in ("enumerate", "zip"):
            if name == "enumerate":
                if not 1 <= len(args) <= 2:
                    raise ValueError("Invalid enumerate arguments")
                result = list(enumerate(self._iter(args[0]), *args[1:]))
            else:
                result = list(zip(*(self._iter(value) for value in args)))
        elif name == "sum":
            if not 1 <= len(args) <= 2:
                raise ValueError("Invalid sum arguments")
            result = args[1] if len(args) == 2 else 0
            for value in self._iter(args[0]):
                result = self._binary(ast.Add(), result, value)
        elif name in ("min", "max", "sorted", "all", "any"):
            if not args:
                raise ValueError("Missing function arguments")
            if name in ("min", "max"):
                function = min if name == "min" else max
                result = function(self._iter(args[0]), **kwargs) if len(args) == 1 else function(*args, **kwargs)
            else:
                if len(args) != 1:
                    raise ValueError("Invalid function arguments")
                functions = {"sorted": sorted, "all": all, "any": any}
                result = functions[name](self._iter(args[0]), **kwargs)
        else:
            functions = {"len": len, "round": round, "abs": abs, "int": int, "float": float, "str": str}
            result = functions[name](*args, **kwargs)
        return self._guard(result)

    def _comprehension(self, node, variables):
        result = {} if isinstance(node, ast.DictComp) else set() if isinstance(node, ast.SetComp) else []

        def collect(index, scope):
            if index == len(node.generators):
                self._tick()
                if len(result) >= MAX_ITEMS:
                    raise ValueError("Collection is too large")
                if isinstance(node, ast.DictComp):
                    key = self._evaluate(node.key, scope)
                    result[key] = self._evaluate(node.value, scope)
                else:
                    item = self._evaluate(node.elt, scope)
                    if isinstance(result, set):
                        result.add(item)
                    else:
                        result.append(item)
                return
            generator = node.generators[index]
            for item in self._iter(self._evaluate(generator.iter, scope)):
                self._tick()
                self._assign(generator.target, item, scope)
                if all(self._evaluate(condition, scope) for condition in generator.ifs):
                    collect(index + 1, scope)

        collect(0, variables.copy())
        return self._guard(result)

    def _evaluate(self, node, variables):
        self._tick()
        if isinstance(node, ast.Constant):
            return self._guard(node.value)
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise ValueError("Unknown calculation variable")
            return variables[node.id]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self._evaluate(item, variables) for item in node.elts]
            return self._guard(tuple(values) if isinstance(node, ast.Tuple) else set(values) if isinstance(node, ast.Set) else values)
        if isinstance(node, ast.Dict):
            return self._guard({self._evaluate(key, variables): self._evaluate(value, variables) for key, value in zip(node.keys, node.values)})
        if isinstance(node, ast.BinOp):
            return self._binary(node.op, self._evaluate(node.left, variables), self._evaluate(node.right, variables))
        if isinstance(node, ast.UnaryOp):
            value = self._evaluate(node.operand, variables)
            if isinstance(node.op, ast.Not):
                return not value
            return self._guard(-value if isinstance(node.op, ast.USub) else +value)
        if isinstance(node, ast.BoolOp):
            for item in node.values:
                value = self._evaluate(item, variables)
                if isinstance(node.op, ast.And) and not value:
                    return value
                if isinstance(node.op, ast.Or) and value:
                    return value
            return value
        if isinstance(node, ast.Compare):
            left = self._evaluate(node.left, variables)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._evaluate(comparator, variables)
                operations = {ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
                              ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right,
                              ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right,
                              ast.In: lambda: left in right, ast.NotIn: lambda: left not in right}
                if not operations[type(operator)]():
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            branch = node.body if self._evaluate(node.test, variables) else node.orelse
            return self._evaluate(branch, variables)
        if isinstance(node, ast.Subscript):
            value = self._evaluate(node.value, variables)
            if type(value) not in (list, tuple, dict, str, range):
                raise ValueError("Value does not support indexing")
            return value[self._evaluate(node.slice, variables)]
        if isinstance(node, ast.Slice):
            return slice(*(self._evaluate(part, variables) if part is not None else None for part in (node.lower, node.upper, node.step)))
        if isinstance(node, ast.Call):
            args = [self._evaluate(arg, variables) for arg in node.args]
            kwargs = {keyword.arg: self._evaluate(keyword.value, variables) for keyword in node.keywords}
            return self._call(node.func.id, args, kwargs)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
            return self._comprehension(node, variables)
        raise ValueError("Unsupported calculation expression")

    def run(self, tree):
        for statement in tree.body:
            value = self._evaluate(statement.value, self.variables)
            for target in statement.targets:
                self._assign(target, value, self.variables)
        if "result" not in self.variables:
            raise ValueError("Calculation must assign result")
        return self.variables["result"]


def _limit_worker(process):
    """Attach a hard Windows memory cap before sending any calculation input."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ValueError("Calculation memory isolation is unavailable")
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x100 | 0x2000 | 0x8  # Process memory, kill on close, one process.
    limits.BasicLimitInformation.ActiveProcessLimit = 1
    limits.ProcessMemoryLimit = WORKER_MEMORY_BYTES
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        kernel.CloseHandle(job)
        raise ValueError("Calculation memory isolation is unavailable")
    return lambda: kernel.CloseHandle(job)


def calculate(query):
    """Return ``{"result": value}`` or raise ValueError with no worker traceback."""
    try:
        if isinstance(query, str):
            if len(query.encode("utf-8")) > MAX_INPUT_BYTES:
                raise ValueError("Calculation input is too large")
            query = json.loads(query)
        if type(query) is not dict:
            raise ValueError("Calculation input must be a JSON object")
        _check_json(query)
        payload = json.dumps(query, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > MAX_INPUT_BYTES:
            raise ValueError("Calculation input is too large")
        _validate(query.get("code"))
    except (TypeError, OverflowError, RecursionError, UnicodeError, MemoryError) as exc:
        raise ValueError("Invalid calculation input") from exc
    environment = {}
    if os.name == "nt":
        # CPython and Windows DLL loading need this directory, never PATH or user configuration.
        environment["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, cwd=Path(sys.executable).resolve().parent,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as exc:
        raise ValueError("Calculation worker is unavailable") from exc
    close_limit = None
    try:
        close_limit = _limit_worker(process)
        output, _ = process.communicate(payload, timeout=WORKER_TIMEOUT_SECONDS)
        if process.returncode != 0:
            raise ValueError("Calculation failed or exceeded memory limits")
        if len(output) > MAX_OUTPUT_BYTES:
            raise ValueError("Calculation output is too large")
        response = json.loads(output)
        if "error" in response:
            raise ValueError(response["error"])
        _check_json(response, output=True)
        return response
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise ValueError("Calculation exceeded the time limit") from exc
    except Exception:
        if process.returncode is None:
            process.kill()
            process.communicate()
        raise
    finally:
        if close_limit:
            close_limit()


def _worker():
    try:
        if os.name != "nt":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
            resource.setrlimit(resource.RLIMIT_CPU, (WORKER_TIMEOUT_SECONDS, WORKER_TIMEOUT_SECONDS))
        payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(payload) > MAX_INPUT_BYTES:
            raise ValueError("Calculation input is too large")
        query = json.loads(payload)
        tree = _validate(query["code"])
        data = query.get("data")
        _check_json(data)
        result = _Evaluator(data).run(tree)
        _check_json(result, output=True)
        response = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(response) > MAX_OUTPUT_BYTES:
            raise ValueError("Calculation output is too large")
    except ValueError:
        # All interpreter-generated ValueErrors are fixed, bounded messages. Native
        # conversion errors may include input text, so return a fixed message.
        response = b'{"error":"Calculation rejected invalid values or exceeded a safety limit"}'
    except (TypeError, KeyError, IndexError, ArithmeticError, RecursionError, MemoryError):
        response = b'{"error":"Calculation failed or exceeded a safety limit"}'
    sys.stdout.buffer.write(response)


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker()
