import tempfile, os
from pathlib import Path
from alpharadar.harvest import harvest, extract_pine_blocks, api_pages, SCRIPTS_FEED

print("--- extract_pine_blocks 单测 ---")
html = '<p>hello</p><pre class="pine">//@version=5\nindicator("x")\nplot(close)</pre>'
print("blocks:", extract_pine_blocks(html))
print("noise ignored:", extract_pine_blocks("<pre>hello world this is not pine at all really</pre>"))

print("--- 真实小规模冒烟（2 页 + 最多下 3 个）---")
with tempfile.TemporaryDirectory() as d:
    stats = {}
    got = harvest(terms=("supertrend",), per_term=10, max_fetch=3,
                  out_dir=Path(d), channels=("search", "feed", "forum"),
                  feed_pages=2, forum_pages=2, progress=lambda *a: None, stats=stats)
    print("returned:", len(got))
    print("stats:", stats)
    files = list((Path(d) / "sources").glob("*.pine"))
    print("files on disk:", len(files))
    one = files[0].read_text(encoding="utf-8")
    print("sample head:", one[:60].replace("\n", " "))
