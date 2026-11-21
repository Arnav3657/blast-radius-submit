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
            if am: files[cur_file]["attrs"].add(am.group(1))
            old_line += 1
        elif raw.startswith("+"):
            content = raw[1:].strip()
            if content and not content.startswith("#"):
                files[cur_file]["comment_only"] = False
            files[cur_file]["lines"].add(old_line)
            am = re.match(r"\s{0,4}([A-Z][A-Z0-9_]{2,})\s*=", raw[1:])
            if am: files[cur_file]["attrs"].add(am.group(1))
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

    with gzip.open(MAP_PATH, "rt", encoding="utf-8") as f:
        cov = json.load(f)
    lines_map = cov["lines"]
    file_tests = cov["file_tests"]
    fixtures = cov["fixtures"]
    funcs_map = cov.get("funcs", {})
    attrs_map = cov.get("attrs", {})

    for patch_path in sorted((DATA / "commits").glob("*.patch")):
        diff_info = parse_patch(patch_path.read_text(encoding="utf-8", errors="ignore"))
        selected: set[int] = set()

        for path, info in diff_info.items():
            if info["comment_only"] and path.endswith(".py"):
                continue

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
                    # Find tightest enclosing function
                    enclosing = [t_ids for s, e, t_ids in f_funcs if s <= ln <= e]
                    if enclosing:
                        # Smallest enclosing function (last or min span)
                        for t_ids in enclosing:
                            hit.update(t_ids)
                    else:
                        touched_top_level = True
                    for delta in (-1, 0, 1):
                        hit.update(f_lines.get(str(ln + delta), ()))

                if touched_top_level or not hit:
                    selected.update(file_tests.get(path, ()))
                else:
                    selected.update(hit)
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                selected.update(tests_by_num.keys())

        chosen = [tests_by_num[n] for n in selected]
        workers = optimal_pack(chosen)
        print(json.dumps({"commit": patch_path.stem, "workers": workers}))


if __name__ == "__main__":
    main()