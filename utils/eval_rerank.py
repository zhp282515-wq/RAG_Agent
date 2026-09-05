"""±Rerank 检索对照评测(可复跑)。

对同一组售后问题,分别走「纯向量召回 get_base_retriever」与「向量召回+Rerank 精排
get_rerank_retriever」,量化两种链路的差异:

  1. 达标率:每问命中「相关度 ≥ 0.60」资料的比例(该门槛与 agent 注入上下文一致,
     见 ReAct/tools/agent_tools.py _SCORE_MIN,<0.60 不注入)。
  2. 达标条数:平均每问达标资料数。
  3. top1 来源准确率:带「期望源文档」的问题里,top1 是否落在期望文档
     (弱主题标签,仅用于观察 Rerank 对弱召回问题的纠正趋势)。

用法:
    .venv/Scripts/python.exe utils/eval_rerank.py [--queries N] [--json logs/rerank_eval.json]

说明:会真实调用 DashScope embedding + rerank API,约几十秒~分钟级。
明细结果写 JSON(默认 logs/rerank_eval.json,已被 .gitignore 忽略,不会入库)。
"""
from __future__ import annotations

import argparse
import json
import time

from dotenv import load_dotenv
load_dotenv(r"C:\Users\Administrator\Desktop\Python-Project\RAG_Agent\.env", override=True)

# 静音控制台 DEBUG(避免逐条检索日志刷屏);文件日志不受影响
from utils import logger_tool
import logging
for _h in logger_tool.logger.handlers:
    if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logger_tool.DailyFileHandler):
        _h.setLevel(logging.WARNING)

from vector_store.vector_store import VectorStoreService


# 期望源文档(强主题规则)。仅给"话题明显落在一份文档"的问题标注;综合/跨文档类标 None。
# 文档主题见知识库实际内容:
#   故障排除.txt  = 故障检测与修复(WiFi/不出水/避障/绑定/回充/尘盒/声音/漏扫…)
#   维护保养.txt  = 保养·耗材·环境(滤网/边刷/电池/异味/梅雨/冬夏/缠绕/噪音分贝…)
#   选购指南.txt  = 选购·对比(滤网vs水洗/买哪种/导航类型/宠物毛发/地毯/禁区?- 选购)
def exp_src(q: str) -> str | None:
    if any(w in q for w in [
        "WiFi", "连不上", "不出水", "绑定", "回不了充电座", "充不进", "漏扫", "少扫",
        "尘盒", "声音", "避障", "摔下", "反复重启", "报错", "不工作", "异响", "开不了机",
        "不响应", "卡住", "不动", "清扫中断", "掉线", "不吸",
    ]):
        return "故障排除.txt"
    if any(w in q for w in [
        "滤网", "边刷", "滚刷", "异味", "梅雨", "夏天", "冬天", "低温", "高温",
        "电池", "充电多久", "换电池", "续航", "缠绕", "清洗", "保养", "污垢", "风干",
        "拖布", "抹布", "静音", "安静",
    ]):
        return "维护保养.txt"
    if any(w in q for w in [
        "买", "选购", "区别", "哪个好", "导航", "扫地还是", "适合", "宠物", "地毯",
        "对比", "哪款",
    ]):
        return "选购指南.txt"
    return None


# ---- 问题集:售后常见问答,六类覆盖 ----
_QA = [  # (问题, 期望源或 None)
    # —— 基础/通用 Q&A ——
    ("扫地机器人第一次使用需要先做什么", None),
    ("扫地机器人的充电时间要多久", None),
    ("机器人在充电座上充不进电怎么办", "故障排除.txt"),
    ("扫地机器人有哪些常见清扫模式", None),
    ("如何看扫地机器人剩余电量", None),
    ("扫地机器人清扫面积怎么看", None),
    ("扫地机器人的耗材多久需要更换一次", None),
    ("扫地机器人可以设置定时清扫吗", None),
    ("扫地机器人工作声音太大正常吗", "故障排除.txt"),
    ("扫地机器人在床底清扫时会不会卡住", "故障排除.txt"),
    ("扫地机器人多久充一次电比较合适", None),
    ("扫地机器人清扫结束后会自动回充吗", None),
    ("扫地机器人吸力大小可以调节吗", None),
    ("扫地机器人的尘盒怎么打开", None),
    ("扫地机器人适合铺什么地板用", None),
    ("扫地机器人能清扫到家具底部吗", None),
    ("扫地机器人清扫前要不要把地面杂物收起来", None),
    ("扫地机器人一般能用几年", None),
    ("扫地机器人的遥控器丢了还能用吗", None),
    ("扫地机器人工作时人可以在家吗", None),
    ("扫地机器人的滚刷多久要清理一次", "维护保养.txt"),
    ("扫地机器人能扫干净瓜子壳吗", None),
    ("扫地机器人会不会把电线卷进去", None),
    ("扫地机器人对身高高的家具会怎么处理", None),
    ("扫地机器人需要联网才能用吗", None),
    ("扫地机器人和吸尘器哪个更省事", "选购指南.txt"),
    ("扫地机器人价格差很多到底差在哪", "选购指南.txt"),
    ("扫地机器人会不会打扰到宠物", None),
    ("扫地机器人扫完地为什么还有灰", "故障排除.txt"),
    ("扫地机器人用的电机是哪种", None),
    ("扫地机器人能不能上地毯清扫", "选购指南.txt"),
    ("扫地机器人的边角清扫效果怎么样", None),
    ("扫地机器人清扫路径为什么是弓字形", None),
    ("扫地机器人会不会把贵重物品撞坏", "故障排除.txt"),
    ("扫地机器人多久需要做一次深度保养", "维护保养.txt"),
    # —— 故障排除类 ——
    ("扫地机器人一直连不上家里的WiFi怎么办", "故障排除.txt"),
    ("手机App绑定扫地机器人失败怎么排查", "故障排除.txt"),
    ("扫地机器人拖地功能不出水怎么解决", "故障排除.txt"),
    ("扫地机器人突然反复重启是什么问题", "故障排除.txt"),
    ("扫地机器人开机没有反应指示灯不亮", "故障排除.txt"),
    ("扫地机器人老是回不了充电座是什么原因", "故障排除.txt"),
    ("扫地机器人清扫到一半就停下来不动", "故障排除.txt"),
    ("扫地机器人避障识别不了拖鞋是什么原因", "故障排除.txt"),
    ("扫地机器人尘盒满了但手机不提示清理", "故障排除.txt"),
    ("扫地机器人碰撞护栏会不会摔下楼梯", "故障排除.txt"),
    ("扫地机器人清扫时总是漏扫墙角怎么办", "故障排除.txt"),
    ("扫地机器人清扫完总是少扫一个房间", "故障排除.txt"),
    ("扫地机器人清扫时异响是什么原因", "故障排除.txt"),
    ("扫地机器人充不进电但充电座指示灯亮", "故障排除.txt"),
    ("扫地机器人自动集尘桩的声音很大正常吗", "故障排除.txt"),
    ("扫地机器人一直提示尘盒未安装", "故障排除.txt"),
    ("扫地机器人边刷不转了怎么修", "故障排除.txt"),
    ("扫地机器人风道堵住吸力变小怎么处理", "故障排除.txt"),
    ("扫地机器人导航失灵乱走是什么原因", "故障排除.txt"),
    ("扫地机器人水箱漏水怎么排查", "故障排除.txt"),
    ("扫地机器人抹布支架卡不住怎么办", None),
    ("扫地机器人找不到充电座一直原地转圈", "故障排除.txt"),
    ("扫地机器人清扫时把数据线卷进去卡死", "故障排除.txt"),
    ("扫地机器人WiFi信号满格却连不上", "故障排除.txt"),
    ("扫地机器人App显示设备离线怎么办", "故障排除.txt"),
    # —— 维护保养/耗材/环境 ——
    ("扫地机器人滤网多久清洗一次", "维护保养.txt"),
    ("扫地机器人的滤网可以直接水洗吗", "维护保养.txt"),
    ("扫地机器人滤网和水洗滤芯哪个更好", "选购指南.txt"),
    ("扫地机器人边刷缠绕头发怎么清理", "维护保养.txt"),
    ("扫地机器人滚刷缠绕头发和线怎么办", "维护保养.txt"),
    ("扫地机器人电池续航变短需要换电池吗", "维护保养.txt"),
    ("扫地机器人长期不用电池要怎么保养", "维护保养.txt"),
    ("扫地机器人拖地后拖布有异味怎么办", "维护保养.txt"),
    ("潮湿梅雨季节扫地机器人要怎么养护", "维护保养.txt"),
    ("冬天低温环境下扫地机器人要特别注意什么", "维护保养.txt"),
    ("夏天高温时扫地机器人能正常工作吗", "维护保养.txt"),
    ("扫地机器人多久需要加润滑油保养", "维护保养.txt"),
    ("扫地机器人的尘盒滤网多久洗一次", "维护保养.txt"),
    ("扫地机器人拖布多久换一次新的", "维护保养.txt"),
    ("扫地机器人主刷两侧的螺丝怎么拆下来清洗", "维护保养.txt"),
    ("扫地机器人如何设置成安静模式", "维护保养.txt"),
    ("扫地机器人拖布烘干功能费电吗", None),
    ("扫地机器人长时间高温暴晒会不会损坏", "维护保养.txt"),
    ("扫地机器人底部传感器脏了怎么清理", "维护保养.txt"),
    ("扫地机器人的尘盒可以用水冲洗吗", "维护保养.txt"),
    ("扫地机器人边刷磨损到什么程度要换", "维护保养.txt"),
    ("扫地机器人用了两年需要做哪些保养", "维护保养.txt"),
    ("扫地机器人毛发防缠绕功能怎么用", "维护保养.txt"),
    ("扫地机器人异味是从哪里来的", "维护保养.txt"),
    ("扫地机器人冬天放阳台会冻坏吗", "维护保养.txt"),
    # —— 扫拖一体专项 ——
    ("扫拖一体机器人拖地后地面有水渍怎么处理", None),
    ("扫拖一体机器人水箱里能加清洁剂吗", None),
    ("扫拖一体机器人的上下水模块怎么接进水管", None),
    ("扫拖一体机器人拖布没装好会提示什么", None),
    ("扫拖一体机器人可以只扫地不拖地吗", None),
    ("扫拖一体机器人的电控水箱怎么调节水量", None),
    ("扫拖一体机器人拖地面积和扫地面积一样吗", None),
    ("扫拖一体机器人银离子除菌模块多久换", "维护保养.txt"),
    ("扫拖一体机器人上下水版适合没有预留水管的家吗", "选购指南.txt"),
    ("扫拖一体机器人对木地板会划伤吗", None),
    # —— 地图/建图/规划 ——
    ("扫地机器人地图建图失败该怎么重新建", None),
    ("扫地机器人清扫禁区怎么在地图上设置", None),
    ("扫地机器人虚拟墙怎么设置", None),
    ("扫地机器人多楼层地图怎么切换", None),
    ("扫地机器人为什么扫完地图会变乱", "故障排除.txt"),
    ("扫地机器人建图时人需要回避吗", None),
    ("扫地机器人搬家后地图要重新建吗", None),
    ("扫地机器人可以把阳台设成禁区吗", None),
    ("扫地机器人更新地图后清扫计划会丢吗", None),
    ("扫地机器人的地图能存几张", None),
    # —— 选购/对比/场景 ——
    ("买扫拖一体机器人还是纯扫地机器人好", "选购指南.txt"),
    ("家里有宠物毛发多适合买哪款扫地机器人", "选购指南.txt"),
    ("激光导航和视觉导航的扫地机器人有什么区别", "选购指南.txt"),
    ("独居小户型买什么扫地机器人合适", "选购指南.txt"),
    ("扫地机器人带自动集尘的好还是不带好", "选购指南.txt"),
    ("扫地机器人的避障能力重要吗", "选购指南.txt"),
    ("家里地毯比较多买扫地机器人要注意什么", "选购指南.txt"),
    ("扫地机器人越贵清扫得越干净吗", "选购指南.txt"),
    ("给父母买扫地机器人要选哪种操作简单的", "选购指南.txt"),
    ("扫地机器人自清洁基座有必要买吗", "选购指南.txt"),
]
# 覆盖度:通用 ~35 / 故障 25 / 保养 25 / 扫拖一体 10 / 建图 10 / 选购 10
assert 100 <= len(_QA) < 120, len(_QA)

_QUERIES = [q for q, _ in _QA]
_EXPECTED = {q: s for q, s in _QA if s}


def run_side(name: str, fn, top_n: int = 5) -> list[dict]:
    rows = []
    for q in _QUERIES:
        hits = fn(q)
        rows.append({"q": q, "hits": hits, "top": hits[:top_n]})
    return rows


def analyze(rows_vec: list[dict], rows_rer: list[dict]) -> dict:
    def met(r):
        # 每问:是否达标 / 达标条数 / 总量
        sat = sum(1 for h in r["hits"] if h["score"] >= 0.60)
        return sat
    sv = [met(r) for r in rows_vec]
    sr = [met(r) for r in rows_rer]
    out = {}
    for label, s in (("vec", sv), ("rer", sr)):
        has = sum(1 for x in s if x > 0)
        out[f"{label}_has"] = has
        out[f"{label}_has_rate"] = round(100 * has / len(s), 1)
        out[f"{label}_avg_sat"] = round(sum(s) / len(s), 2)
        out[f"{label}_sat"] = s
    # 归因:top1 是否命中期望源
    cmp = 0; hv = 0; hr = 0
    for rv, rr in zip(rows_vec, rows_rer):
        ed = exp_src(rv["q"])
        if ed is None:
            continue
        cmp += 1
        v1 = rv["hits"][0]["file_name"] if rv["hits"] else "-"
        r1 = rr["hits"][0]["file_name"] if rr["hits"] else "-"
        hv += (v1 == ed); hr += (r1 == ed)
    out["attr_cmp"] = cmp
    out["attr_vec_hit"] = hv
    out["attr_rer_hit"] = hr
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="logs/rerank_eval.json")
    args = ap.parse_args()

    vec = VectorStoreService()
    base = vec.get_base_retriever(top_k=20)
    rer = vec.get_rerank_retriever(top_k=20, rerank_n=5)

    t0 = time.time()
    rows_vec = run_side("vec", base)
    t1 = time.time()
    rows_rer = run_side("rer", rer)
    t2 = time.time()
    stats = analyze(rows_vec, rows_rer)

    print(f"\n已跑 {len(_QUERIES)} 问 | 纯向量 {t1-t0:.0f}s | rerank 另加 {t2-t1:.0f}s")
    print(f"  有达标(≥0.60)资料的问题: 纯向量 {stats['vec_has']}/{len(_QUERIES)} "
          f"({stats['vec_has_rate']}%) | Rerank {stats['rer_has']}/{len(_QUERIES)} "
          f"({stats['rer_has_rate']}%)")
    print(f"  平均每问达标条数:          纯向量 {stats['vec_avg_sat']} | Rerank {stats['rer_avg_sat']}")
    print(f"  top1 命中期望源(可比 {stats['attr_cmp']} 问): 纯向量 "
          f"{stats['attr_vec_hit']} ({100*stats['attr_vec_hit']/max(1,stats['attr_cmp']):.0f}%) | "
          f"Rerank {stats['attr_rer_hit']} ({100*stats['attr_rer_hit']/max(1,stats['attr_cmp']):.0f}%)")

    with open(args.json, "w", encoding="utf-8") as f:
        json.dump({
            "n": len(_QUERIES), "stats": stats,
            "vec": rows_vec, "rer": rows_rer,
        }, f, ensure_ascii=False, default=str)
    print(f"明细已存 {args.json}")


if __name__ == "__main__":
    main()
