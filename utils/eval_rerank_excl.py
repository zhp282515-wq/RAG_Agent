"""实验:排除两份通用大题库(120问.md/100问.pdf)后,rerank 相对 vec 的净增益。

背景:200 问全库对照显示,rerank 把 top1 从专题文档纠偏到故障/保养/选购有收益,但对
「扫拖一体100问.txt」与两份通用大题库反而变差(top1 被语义相近的通用大题库拽走,挤掉
同源片)。本实验把检索范围限定在 4 份专题文档(故障排除/维护保养/选购指南/扫拖一体100问),
观察在这种「信任专题文档」口径下 rerank 是否仍有增益。

对每问,file_names 白名单 = 该问期望源所属的专题文档(带期望源的 182 问)。纯向量(vec)
与 rerank(rer)在同白名单内对比,归因基准即 _EXPECTED。

用法:
    .venv/Scripts/python.exe -m utils.eval_rerank_excl --json logs/rerank_eval_excl_200.json

说明:会真实调用 DashScope embedding + rerank,与主评测同样分钟级。结果不覆盖主评测 JSON。
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter

from utils.eval_rerank import _QS, _EXPECTED, _sat, analyze  # 复用题集与统计口径

from vector_store.vector_store import VectorStoreService

# 被排除的通用大题库(不参与检索);实验只在剩余 4 份专题文档内搜
_EXCLUDED = ["扫地机器人120问.md", "扫地机器人100问.pdf"]
_SPECIAL_DOCS = ["故障排除.txt", "维护保养.txt", "选购指南.txt", "扫拖一体机器人100问.txt"]


def _run(fn, whitelist_for, top_n: int = 5) -> list[dict]:
    rows = []
    for q in _QS:
        wl = whitelist_for(q)
        hits = fn(q) if wl is None else fn(q, file_names=wl)
        rows.append({"q": q, "hits": hits, "top": hits[:top_n]})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="logs/rerank_eval_excl_200.json")
    args = ap.parse_args()

    vec = VectorStoreService()
    base = vec.get_base_retriever(top_k=20)
    rer = vec.get_rerank_retriever(top_k=20, rerank_n=5)

    # 带期望源的问题才限定专题文档;综合题(None)保持全库,不参与归因但参与达标统计
    def wl_for(q: str):
        ed = _EXPECTED.get(q)
        return None if ed is None else _SPECIAL_DOCS

    print(f"问题集 {len(_QS)} 问 | 限定 {len(_SPECIAL_DOCS)} 份专题文档 "
          f"(排除 {_EXCLUDED}) | 带期望源 {len(_EXPECTED)} 问")
    t0 = time.time()
    rows_vec = _run(base, wl_for); t1 = time.time()
    rows_rer = _run(rer, wl_for); t2 = time.time()
    stats = analyze(rows_vec, rows_rer)

    print(f"\n已跑 {len(_QS)} 问 | 纯向量 {t1-t0:.0f}s | rerank 另加 {t2-t1:.0f}s")
    print(f"  有达标(≥0.60): vec {stats['vec_has']}/{len(_QS)} ({stats['vec_has_rate']}%) "
          f"| rer {stats['rer_has']}/{len(_QS)} ({stats['rer_has_rate']}%)")
    print(f"  平均每问达标条数: vec {stats['vec_avg_sat']} | rer {stats['rer_avg_sat']}")
    print(f"  top1 命中期望源({stats['attr_cmp']} 可比): vec {stats['attr_vec_hit']} "
          f"| rer {stats['attr_rer_hit']}")
    print("  按期望源拆解(top1 命中/该源题数,rerank):")
    for src in sorted(stats["attr_by_src"], key=lambda s: -stats["attr_by_src"][s]["n"]):
        b = stats["attr_by_src"][src]
        print(f"    {src:<22} rer {b['rer']}/{b['n']} | vec {b['vec']}/{b['n']}")

    with open(args.json, "w", encoding="utf-8") as f:
        json.dump({"n": len(_QS), "excluded": _EXCLUDED, "stats": stats,
                   "vec": rows_vec, "rer": rows_rer}, f,
                  ensure_ascii=False, default=str)
    print(f"明细已存 {args.json}")


if __name__ == "__main__":
    main()
