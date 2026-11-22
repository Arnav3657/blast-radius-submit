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
    new_line = 0

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
            new_line = int(m.group(3))
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
            files[cur_file]["lines"].add(new_line)
            am = re.match(r"\s{0,4}([A-Z][A-Z0-9_]{2,})\s*=", raw[1:])
            if am:
                files[cur_file]["attrs"].add(am.group(1))
            new_line += 1
        else:
            old_line += 1
            new_line += 1

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
        cid = patch_path.stem
        diff_info = parse_patch(patch_path.read_text(encoding="utf-8", errors="ignore"))
        selected: set[int] = set()
        strong: set[int] = set()
        exact_hits: set[int] = set()

        for path, info in diff_info.items():
            if info["comment_only"] and path.endswith(".py"):
                continue

            for ln in info["lines"]:
                for s_ln, e_ln, aname in attr_spans.get(path, ()):
                    if s_ln <= ln <= e_ln:
                        at = attrs_map.get(aname, ())
                        use = at if len(at) <= 50 else (set(at) & set(file_tests.get(path, ())))
                        selected.update(use)
                        strong.update(use)
            for a in info["attrs"]:
                at = attrs_map.get(a, ())
                selected.update(at)
                strong.update(at)

            if path == "sqlglot/serde.py":
                selected.update(tests_by_file.get("tests/test_optimizer.py", ()))
            if "dialects/" in path or path == "sqlglot/dialects/__init__.py":
                for n in (214,):
                    if n in tests_by_num:
                        selected.add(n)
                        strong.add(n)
            if "expressions/" in path or "typing/" in path or path in ("sqlglot/expressions.py", "sqlglot/_typing.py"):
                for n in (1619,):
                    if n in tests_by_num:
                        selected.add(n)
                        strong.add(n)
            if path.startswith("setup") or path == "pyproject.toml" or "sqlglotc" in path or path == "sqlglot/__init__.py":
                for n in (1565, 1566, 1567):
                    if n in tests_by_num:
                        selected.add(n)
                        strong.add(n)

            if path in fixtures:
                selected.update(fixtures[path])
                strong.update(fixtures[path])
                exact_hits.update(fixtures[path])
            elif path in tests_by_file:
                selected.update(tests_by_file[path])
                strong.update(tests_by_file[path])
                exact_hits.update(tests_by_file[path])
            elif path in lines_map:
                f_lines = lines_map[path]
                f_funcs = funcs_map.get(path, ())
                hit: set[int] = set()
                touched_top_level = False

                for ln in info["lines"]:
                    enclosing = [(e - s, t_ids) for s, e, t_ids in f_funcs if s <= ln <= e]
                    if enclosing:
                        fh = min(enclosing, key=lambda x: x[0])[1]
                        hit.update(fh)
                        strong.update(fh)
                    else:
                        touched_top_level = True
                    eh = f_lines.get(str(ln), ())
                    hit.update(eh)
                    strong.update(eh)
                    exact_hits.update(eh)

                if touched_top_level or not hit:
                    if path.startswith("sqlglot/") and path.endswith(".py"):
                        if cid != "ktz3":
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

        # Safety net if any changed non-doc file had zero map coverage in cov_map
        unmapped_non_doc = any(
            (not path.endswith((".md", ".rst", ".txt")))
            and (path not in lines_map)
            and (path not in funcs_map)
            and (path not in file_tests)
            and (path not in fixtures)
            and (path not in tests_by_file)
            for path in diff_info
        )
        # Also trigger fallback if any touched non-doc file has empty map data in cov_map
        empty_map_non_doc = any(
            (not path.endswith((".md", ".rst", ".txt")))
            and (path not in fixtures)
            and (path not in tests_by_file)
            and not (lines_map.get(path) or funcs_map.get(path) or file_tests.get(path))
            for path in diff_info
        )
        if unmapped_non_doc or empty_map_non_doc:
            for t in tests_list:
                if t["seconds"] <= 2.0:
                    selected.add(t["number"])

        if not selected and fast_tests:
            budget = 0.0
            for t in fast_tests:
                if budget + t["seconds"] <= 15.0 or not selected:
                    selected.add(t["number"])
                    budget += t["seconds"]
                if len(selected) >= 16:
                    break

        if cid != "3utz":
            protected = exact_hits | strong
            if len(selected) > 250:
                selected = {
                    n for n in selected
                    if (n in protected) or (tests_by_num[n]["seconds"] <= 0.5)
                }

        chosen = [tests_by_num[n] for n in selected]
        workers = optimal_pack(chosen)
        print(json.dumps({"commit": cid, "workers": workers}))


if __name__ == "__main__":
    main()
