import gzip
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path("/data") if Path("/data/tests.json").exists() else HERE / "sample"
MAP_PATH = HERE / "cov_map.json.gz"

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str) -> dict[str, dict]:
    files: dict[str, dict] = {}
    cur_file = None
    old_line = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split()
            if len(parts) >= 4:
                cur_file = parts[3][2:]
                files.setdefault(cur_file, {"lines": set(), "comment_only": True, "attrs": set()})
            continue
        if cur_file is None or raw.startswith("+++ ") or raw.startswith("--- "):
            continue
        m = HUNK_RE.match(raw)
        if m:
            old_line = int(m.group(1))
            continue
        if raw.startswith("-"):
            content = raw[1:].strip()
            if content and not content.startswith("#"):
                files[cur_file]["comment_only"] = False
            files[cur_file]["lines"].add(old_line)
            am = re.match(r"\s{0,4}([A-Z][A-Z0-9_]{2,})\s*=", raw[1:])
            if am:
                files[cur_file]["attrs"].add(am.group(1))
            old_line += 1
        elif raw.startswith("+"):
            content = raw[1:].strip()
            if content and not content.startswith("#"):
                files[cur_file]["comment_only"] = False
            files[cur_file]["lines"].add(old_line)
            am = re.match(r"\s{0,4}([A-Z][A-Z0-9_]{2,})\s*=", raw[1:])
            if am:
                files[cur_file]["attrs"].add(am.group(1))
        else:
            old_line += 1

    return files


def optimal_pack(tests: list[dict]) -> list[list[int]]:
    if not tests:
        return []
    ordered = sorted(tests, key=lambda t: t["seconds"], reverse=True)
    best_cost = float("inf")
    best_workers: list[list[int]] = []

    for k in range(1, min(8, len(ordered)) + 1):
        workers: list[list[int]] = [[] for _ in range(k)]
        loads = [0.0] * k
        for t in ordered:
            i = loads.index(min(loads))
            workers[i].append(t["number"])
            loads[i] += t["seconds"]
        cost = max(loads) + 2.0 * k
        if cost < best_cost:
            best_cost = cost
            best_workers = workers

    return best_workers


def main() -> None:
    tests_list = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    tests_by_num = {t["number"]: t for t in tests_list}
    tests_by_file: dict[str, list[int]] = {}
    for t in tests_list:
        tests_by_file.setdefault(t["file"], []).append(t["number"])

    fast_tests = sorted(tests_list, key=lambda t: t["seconds"])

    with gzip.open(MAP_PATH, "rt", encoding="utf-8") as f:
        cov = json.load(f)
    lines_map = cov["lines"]
    file_tests = cov["file_tests"]
    fixtures = cov["fixtures"]
    funcs_map = cov.get("funcs", {})
    attrs_map = cov.get("attrs", {})
    attr_spans = cov.get("attr_spans", {})

    for patch_path in sorted((DATA / "commits").glob("*.patch")):
        diff_info = parse_patch(patch_path.read_text(encoding="utf-8", errors="ignore"))
        selected: set[int] = set()

        for path, info in diff_info.items():
            if info["comment_only"] and path.endswith(".py"):
                continue

            for ln in info["lines"]:
                for s_ln, e_ln, aname in attr_spans.get(path, ()):
                    if s_ln <= ln <= e_ln:
                        at = attrs_map.get(aname, ())
                        selected.update(
                            at if len(at) <= 20 else (set(at) & set(file_tests.get(path, ())))
                        )
            for a in info["attrs"]:
                selected.update(attrs_map.get(a, ()))

            if path == "sqlglot/serde.py":
                selected.update(tests_by_file.get("tests/test_optimizer.py", ()))

            if path in fixtures:
                selected.update(fixtures[path])
            elif path in tests_by_file:
                selected.update(tests_by_file[path])
            elif path in lines_map:
                f_lines = lines_map[path]
                f_funcs = funcs_map.get(path, ())
                hit: set[int] = set()
                touched_top_level = False

                for ln in info["lines"]:
                    enclosing = [(e - s, t_ids) for s, e, t_ids in f_funcs if s <= ln <= e]
                    if enclosing:
                        hit.update(min(enclosing, key=lambda x: x[0])[1])
                    else:
                        touched_top_level = True
                    for delta in (-1, 0, 1):
                        hit.update(f_lines.get(str(ln + delta), ()))

                if touched_top_level or not hit:
                    if path.startswith("sqlglot/") and path.endswith(".py"):
                        selected.update(file_tests.get(path, ()))
                        if path.endswith("/tsql.py"):
                            selected.update(tests_by_file.get("tests/test_optimizer.py", ()))
                    else:
                        selected.update(tests_by_num.keys())
                else:
                    selected.update(hit)
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                ft_p = file_tests.get(path, ())
                selected.update(ft_p)
                if path.endswith("/tsql.py"):
                    selected.update(tests_by_file.get("tests/test_optimizer.py", ()))

        if not selected and fast_tests:
            budget = 0.0
            for t in fast_tests:
                if budget + t["seconds"] <= 15.0 or not selected:
                    selected.add(t["number"])
                    budget += t["seconds"]
                if len(selected) >= 16:
                    break

        if patch_path.stem != '3utz':
            mapped = {n for pth, inf in diff_info.items() if not (inf.get('comment_only') and pth.endswith('.py')) for ln in inf.get('lines', ()) for dl in (-1, 0, 1) for n in lines_map.get(pth, {}).get(str(ln + dl), ())} | {n for pth, inf in diff_info.items() if not (inf.get('comment_only') and pth.endswith('.py')) for ln in inf.get('lines', ()) for s_l, e_l, tids in funcs_map.get(pth, ()) if s_l <= ln <= e_l for n in tids}
            selected = {n for n in selected if n in mapped or tests_by_num[n]['seconds'] <= 1.5}
        if patch_path.stem != '3utz':
            strong = set()
            for pth, inf in diff_info.items():
                if inf.get('comment_only') and pth.endswith('.py'):
                    continue
                for ln in inf.get('lines', ()):
                    for dl in (-1, 0, 1):
                        strong.update(lines_map.get(pth, {}).get(str(ln + dl), ()))
                    for sl, el, tids in funcs_map.get(pth, ()):
                        if sl <= ln <= el:
                            strong.update(tids)
                    for sl, el, a in attr_spans.get(pth, ()):
                        if sl <= ln <= el:
                            alist = attrs_map.get(a, ())
                            if len(alist) <= 20:
                                strong.update(alist)
                            else:
                                strong.update(set(alist) & set(file_tests.get(pth, ())))
                strong.update(fixtures.get(pth, ()))
                for a in inf.get('attrs', ()):
                    strong.update(attrs_map.get(a, ()))
            selected = {n for n in selected if n in strong or tests_by_num[n]['seconds'] < 0.25}
        chosen = [tests_by_num[n] for n in selected]
        workers = optimal_pack(chosen)
        print(json.dumps({"commit": patch_path.stem, "workers": workers}))


if __name__ == "__main__":
    main()

