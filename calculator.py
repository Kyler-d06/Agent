"""Bounded numeric AST calculator shared by the utility and regression engine."""
import ast
import math
import operator

FUNCTIONS = {n: getattr(math, n) for n in (
    "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "log", "log10", "log2", "exp", "floor", "ceil", "fabs", "degrees", "radians",
)} | {"abs": abs, "round": round, "min": min, "max": max}
CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
        raise ValueError("numeric value outside supported range")
    return value


def _evaluate(node):
    if isinstance(node, ast.Constant):
        return _number(node.value)
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _number(_evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1))
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 1000 or (left and abs(left) != 1 and right * math.log10(abs(left)) > 100):
                raise ValueError("power exceeds calculation budget")
            return _number(left ** right)
        if type(node.op) in OPS:
            return _number(OPS[type(node.op)](left, right))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.keywords or len(node.args) > 16:
            raise ValueError("unsupported function arguments")
        args = [_evaluate(a) for a in node.args]
        if node.func.id == "factorial":
            if len(args) != 1 or type(args[0]) is not int or not 0 <= args[0] <= 69:
                raise ValueError("factorial supports integers 0 through 69")
            return _number(math.factorial(args[0]))
        if node.func.id in FUNCTIONS:
            return _number(FUNCTIONS[node.func.id](*args))
    raise ValueError("unsupported expression")


def calculate(expression: str) -> str:
    """Evaluate bounded numeric arithmetic; commas separate function arguments."""
    try:
        if not isinstance(expression, str) or len(expression) > 2048:
            raise ValueError("expression exceeds calculation budget")
        tree = ast.parse(expression.strip().replace("×", "*").replace("÷", "/").replace("^", "**"), mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 128:
            raise ValueError("expression exceeds calculation budget")
        return str(_evaluate(tree.body))
    except ZeroDivisionError:
        return "Error: division by zero"
    except Exception as exc:
        return f"Calc error: {exc}"
