"""
PharmaData Agent — Evaluation Pipeline (async + interrupt-aware)
=================================================================
Usage:
    python eval_pipeline.py [--dataset path/to/dataset.json] [--output results/] [--ids EX01,EX03]
"""

import os
import json
import time
import uuid
import asyncio
import argparse
from dotenv import load_dotenv
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
from copy import deepcopy
import tempfile
import shutil

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.types import Command
from src.agent import graph
from src.data_manager import ExcelDataManager

load_dotenv()

def make_config(thread_id: str, data_manager) -> dict:
    return {"configurable": {"thread_id": thread_id, "data_manager": data_manager}, "recursion_limit": 20}


async def call_agent(question: str, config: dict) -> Tuple[str,dict]:
    if graph is None:
        raise RuntimeError("graph is not imported.")

    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=question)]},
        config=config,
    )

    if "__interrupt__" in result:
        print("     ⏸  Interrupt detected → auto-approving write...")
        result = await graph.ainvoke(Command(resume="approve"), config=config)

    messages = result.get("messages", [])
    if not messages:
        return "[NO RESPONSE]"

    number_messages = len(messages)
    last_human_msg = 0
    for idx, msg in enumerate(reversed(messages)):
      if isinstance(msg,HumanMessage):
          last_human_msg = idx
          break
    metadata_total = {"input_tokens": 0, "output_tokens": 0}
    for msg in messages[number_messages - last_human_msg -1:]:
        if isinstance(msg,AIMessage):
          metadata = msg.usage_metadata
          metadata_total["input_tokens"] += metadata["input_tokens"]
          metadata_total["output_tokens"] += metadata["output_tokens"]
      
    last = messages[-1]
    return last.content if hasattr(last, "content") else str(last) , metadata_total


JUDGE_SYSTEM_PROMPT = """You are a strict but fair evaluator for a pharmaceutical data assistant.

Your job is to decide whether the AGENT ANSWER correctly answers the QUESTION
given the EXPECTED ANSWER as ground truth.

Rules:
- The agent answer does not need to match word-for-word, but must contain the correct key facts and numbers.
- Numerical values must be accurate (minor rounding <= 1% is acceptable).
- Ignore formatting differences (markdown vs plain text, order of list items).
- Be strict about wrong numbers or hallucinated data.

Respond with ONLY a valid JSON object (no markdown fences, no explanation) with this exact schema:
{"score": <0|1|2>, "verdict": "<CORRECT|PARTIAL|WRONG>", "reason": "<one concise sentence>"}

Score scale:
  2 = CORRECT  - All key facts and numbers are right.
  1 = PARTIAL  - Partially correct, missing some facts or minor numerical errors.
  0 = WRONG    - Factually incorrect or important data is hallucinated/missing.
"""

JUDGE_USER_TEMPLATE = """QUESTION:
{question}

EXPECTED ANSWER:
{expected}

AGENT ANSWER:
{agent_answer}
"""


def build_judge() -> ChatOpenAI:
    return ChatOpenAI(
        model="arcee-ai/trinity-large-preview:free",
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
        temperature=0,
    )


def judge_response(llm: ChatOpenAI, question: str, expected: str, agent_answer: str) -> dict:
    user_msg = JUDGE_USER_TEMPLATE.format(
        question=question, expected=expected, agent_answer=agent_answer,
    )
    for attempt in range(3):
        try:
            resp = llm.invoke([
                SystemMessage(content=JUDGE_SYSTEM_PROMPT),
                HumanMessage(content=user_msg),
            ])
            raw = resp.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as e:
            if attempt == 2:
                return {"score": -1, "verdict": "JUDGE_ERROR", "reason": str(e)}
            time.sleep(1)


SKIP_MARKER = "none"


async def run_exercise(exercise: dict, judge: ChatOpenAI, data_manager: ExcelDataManager) -> dict:
    ex_id       = exercise["id"]
    difficulty  = exercise["difficulty"]
    ex_type     = exercise["type"]
    questions   = exercise["questions"]
    answers     = exercise["answers"]
    description = exercise.get("description", "")

    thread_id = str(uuid.uuid4())
    config    = make_config(thread_id, data_manager)

    turn_results = []

    for idx, (question, expected) in enumerate(zip(questions, answers)):
        is_skipped = expected.strip().lower() == SKIP_MARKER
        skip_tag   = " [SKIP]" if is_skipped else ""
        print(f"  Q{idx+1}{skip_tag}: {question[:72]}{'...' if len(question)>72 else ''}")

        t0 = time.perf_counter()
        try:
            agent_answer, metadata  = await call_agent(question, config)
            latency = round(time.perf_counter() - t0, 2)
            error   = None
        except Exception as e:
            agent_answer = f"[AGENT ERROR: {e}]"
            latency = round(time.perf_counter() - t0, 2)
            metadata= None
            error   = str(e)

        if is_skipped:
            judgment = {
                "score":   None,
                "verdict": "SKIPPED",
                "reason":  "Expected answer is 'none' — turn not evaluated.",
            }
            icon = "⏭ "
        elif error:
            judgment = {"score": 0, "verdict": "AGENT_ERROR", "reason": error}
            icon = "🔴"
        else:
            judgment = await asyncio.to_thread(
                judge_response, judge, question, expected, agent_answer
            )
            icon = {
                "CORRECT":     "✅",
                "PARTIAL":     "⚠️ ",
                "WRONG":       "❌",
                "JUDGE_ERROR": "🔴",
            }.get(judgment.get("verdict", "?"), "❓")

        verdict = judgment.get("verdict", "?")
        score   = judgment.get("score")
        reason  = judgment.get("reason", "")
        print(f"       {icon} {verdict}  ({latency}s)  — {reason}")

        turn_results.append({
            "turn":         idx + 1,
            "question":     question,
            "expected":     expected,
            "agent_answer": agent_answer,
            "metadata":     metadata,
            "score":        score,
            "verdict":      verdict,
            "reason":       reason,
            "latency_s":    latency,
            "skipped":      is_skipped,
        })

    scored      = [t for t in turn_results if t["score"] is not None and t["score"] >= 0]
    total_score = sum(t["score"] for t in scored)
    max_score   = len(scored) * 2
    pct         = round(100 * total_score / max_score, 1) if max_score else 0
    total_metadata = {
        "input_tokens":  sum(t["metadata"]["input_tokens"]  for t in turn_results if t["metadata"]),
        "output_tokens": sum(t["metadata"]["output_tokens"] for t in turn_results if t["metadata"]),
    }
    
    return {
        "id":             ex_id,
        "difficulty":     difficulty,
        "type":           ex_type,
        "description":    description,
        "total_metadata": total_metadata,
        "thread_id":      thread_id,
        "turns":          turn_results,
        "total_score":    total_score,
        "max_score":      max_score,
        "pct":            pct,
    }


async def run_pipeline(
    dataset_path: str,
    excel_path: str,
    output_dir: str,
    filter_ids: Optional[list] = None,
) -> dict:
    dataset_path = Path(dataset_path)
    output_dir   = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(dataset_path) as f:
        exercises = json.load(f)

    if filter_ids:
        exercises = [e for e in exercises if e["id"] in filter_ids]

    judge      = build_judge()
    results    = []
    start_time = datetime.now()

    print(f"\n{'═'*62}")
    print(f"  PharmaData Agent — Evaluation Pipeline")
    print(f"  Dataset  : {dataset_path.name}  ({len(exercises)} exercises)")
    print(f"  Start    : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Strategy : fresh thread per exercise | auto-approve writes")
    print(f"{'═'*62}\n")
    
    data_manager = ExcelDataManager(excel_path)

    for ex in exercises:
        n_turns  = len(ex["questions"])
        n_skip   = sum(1 for a in ex["answers"] if a.strip().lower() == SKIP_MARKER)
        n_judged = n_turns - n_skip
        print(
            f"[{ex['id']}] {ex['difficulty'].upper():6s} {ex['type']:22s} "
            f"— {ex.get('description','')}"
        )
        print(f"  turns: {n_turns}  |  judged: {n_judged}  |  skipped: {n_skip}")

        try:
            result = await run_exercise(ex, judge, deepcopy(data_manager))
            results.append(result)
            print(
                f"  → Score: {result['total_score']}/{result['max_score']} "
                f"({result['pct']}%)\n"
            )
        except RuntimeError as e:
            print(f"\n  ⛔  {e}\n")
            raise SystemExit(1)
        except Exception as e:
            print(f"  ⛔  Unexpected error: {e}\n")
            results.append({
                "id": ex["id"], "difficulty": ex["difficulty"],
                "type": ex["type"], "description": ex.get("description",""),
                "thread_id": "N/A", "turns": [],
                "total_score": 0, "max_score": 0, "pct": 0,
                "total_metadata": {"input_tokens": 0, "output_tokens": 0},
                "error": str(e),
            })

    # ── aggregate stats ──
    total_score = sum(r["total_score"] for r in results)
    max_score   = sum(r["max_score"]   for r in results)
    pct_overall = round(100 * total_score / max_score, 1) if max_score else 0

    by_difficulty, by_type = {}, {}
    for r in results:
        for key, mapping in [(r["difficulty"], by_difficulty), (r["type"], by_type)]:
            g = mapping.setdefault(key, {
                "score": 0, "max": 0, "count": 0,
                "input_tokens": 0, "output_tokens": 0, "n_turns": 0,
            })
            g["score"] += r["total_score"]
            g["max"]   += r["max_score"]
            g["count"] += 1
            tm = r.get("total_metadata", {})
            g["input_tokens"]  += tm.get("input_tokens",  0)
            g["output_tokens"] += tm.get("output_tokens", 0)
            g["n_turns"]       += len(r.get("turns", []))

    for g in list(by_difficulty.values()) + list(by_type.values()):
        g["pct"] = round(100 * g["score"] / g["max"], 1) if g["max"] else 0
        n = g["n_turns"] or 1
        g["avg_input_per_q"]  = round(g["input_tokens"]  / n)
        g["avg_output_per_q"] = round(g["output_tokens"] / n)

    total_input_tokens  = sum(r.get("total_metadata", {}).get("input_tokens",  0) for r in results)
    total_output_tokens = sum(r.get("total_metadata", {}).get("output_tokens", 0) for r in results)
    total_turns         = sum(len(r.get("turns", [])) for r in results)
    avg_in_q            = round(total_input_tokens  / total_turns) if total_turns else 0
    avg_out_q           = round(total_output_tokens / total_turns) if total_turns else 0

    summary = {
        "run_date":            start_time.isoformat(),
        "dataset":             str(dataset_path),
        "total_score":         total_score,
        "max_score":           max_score,
        "pct_overall":         pct_overall,
        "total_input_tokens":  total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_turns":         total_turns,
        "by_difficulty":       by_difficulty,
        "by_type":             by_type,
        "exercises":           results,
    }

    ts        = start_time.strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"eval_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n📄 JSON results → {json_path}")

    print(f"\n{'═'*62}")
    print(f"  OVERALL : {total_score}/{max_score}  ({pct_overall}%)")
    print(f"  TOKENS  : {total_input_tokens:,} in / {total_output_tokens:,} out  (avg/Q: {avg_in_q:,} in / {avg_out_q:,} out)")
    print(f"{'═'*62}")
    print("\n  By difficulty:")
    for diff in ["easy", "medium", "hard"]:
        if diff in by_difficulty:
            g   = by_difficulty[diff]
            bar = "█" * int(g["pct"] // 5) + "░" * (20 - int(g["pct"] // 5))
            print(f"    {diff:8s}  [{bar}] {g['pct']:5.1f}%  ({g['score']}/{g['max']})  "
                  f"~{g['avg_input_per_q']:,} in / {g['avg_output_per_q']:,} out per Q")
    print("\n  By type:")
    for t, g in sorted(by_type.items(), key=lambda x: -x[1]["pct"]):
        print(f"    {t:22s}  {g['pct']:5.1f}%  ({g['score']}/{g['max']})")

    html_path = output_dir / f"eval_{ts}.html"
    generate_html_report(summary, html_path)
    print(f"\n🌐 HTML report    → {html_path}\n")

    return summary


def _sc(p):
    return "#22c55e" if p >= 80 else "#f59e0b" if p >= 50 else "#ef4444"

def _badge(v):
    cfg = {
        "CORRECT":     ("#22c55e", "✓"),
        "PARTIAL":     ("#f59e0b", "~"),
        "WRONG":       ("#ef4444", "✗"),
        "SKIPPED":     ("#6b7280", "⏭"),
        "AGENT_ERROR": ("#8b5cf6", "⚡"),
        "JUDGE_ERROR": ("#475569", "?"),
    }
    c, i = cfg.get(v, ("#475569", "?"))
    return f'<span class="badge" style="background:{c}">{i} {v}</span>'

def _chip(d):
    cfg = {"easy":("#bbf7d0","#166534"),"medium":("#fef08a","#854d0e"),"hard":("#fecaca","#991b1b")}
    bg, tc = cfg.get(d, ("#e5e7eb","#111"))
    return f'<span class="chip" style="background:{bg};color:{tc}">{d}</span>'

def _bar(val, color):
    w = max(0, min(100, val))
    return (f'<div class="bar-wrap"><div class="bar" style="width:{w}%;background:{color}">'
            f'</div><span>{val:.1f}%</span></div>')

def _tok(n: int) -> str:
    """Format token count with narrow no-break space as thousands separator."""
    return f"{n:,}".replace(",", "\u202f")


def generate_html_report(summary: dict, output_path: Path):
    exercises = summary["exercises"]
    total_ex  = len(exercises)
    pct       = summary["pct_overall"]

    all_turns = [t for r in exercises for t in r.get("turns", [])]
    verdicts  = {}
    for t in all_turns:
        v = t.get("verdict","?")
        verdicts[v] = verdicts.get(v,0) + 1

    cards_html = ""
    for r in exercises:
        ex_meta = r.get("total_metadata", {})
        ex_in   = ex_meta.get("input_tokens",  0)
        ex_out  = ex_meta.get("output_tokens", 0)

        turns_html = ""
        for t in r.get("turns", []):
            v       = t.get("verdict","?")
            skipped = t.get("skipped", False)
            dim     = ' style="opacity:.5"' if skipped else ""
            s_txt   = "SKIP" if skipped else (f"{t['score']}/2" if t['score'] is not None else "ERR")
            t_meta  = t.get("metadata") or {}
            t_in    = t_meta.get("input_tokens",  0)
            t_out   = t_meta.get("output_tokens", 0)
            tok_html = (
                f'<span class="tok-badge" title="Tokens envoyés au LLM">⬆ {_tok(t_in)} tk</span>'
                f'<span class="tok-badge tok-out" title="Tokens reçus du LLM">⬇ {_tok(t_out)} tk</span>'
            ) if (t_in or t_out) else ""
            turns_html += f"""
            <div class="turn"{dim}>
              <div class="turn-header">
                <span class="turn-num">Q{t['turn']}</span>
                {_badge(v)}
                <span class="score-mini">{s_txt}</span>
                {tok_html}
                <span class="latency">{t.get('latency_s','?')}s</span>
              </div>
              <div class="turn-body">
                <div class="field"><label>Question</label><p>{t['question']}</p></div>
                <div class="turn-cols">
                  <div class="field"><label>Expected</label><pre>{t['expected']}</pre></div>
                  <div class="field"><label>Agent Answer</label><pre>{t['agent_answer']}</pre></div>
                </div>
                <div class="reason-box">💬 {t.get('reason','')}</div>
              </div>
            </div>"""

        skip_n    = sum(1 for t in r.get("turns",[]) if t.get("skipped"))
        skip_note = f' <span class="skip-note">({skip_n} skipped)</span>' if skip_n else ""
        tok_note  = (
            f'<span class="ex-tok" title="{_tok(ex_in)} tokens in / {_tok(ex_out)} tokens out">'
            f'⬆ {_tok(ex_in)} tk &nbsp;⬇ {_tok(ex_out)} tk</span>'
        )
        cards_html += f"""
        <div class="card" id="{r['id']}" data-diff="{r['difficulty']}" data-type="{r['type']}">
          <div class="card-header" onclick="toggle(this)">
            <div class="card-title">
              <span class="ex-id">{r['id']}</span>
              {_chip(r['difficulty'])}
              <span class="type-tag">{r['type']}</span>
              <span class="desc">{r.get('description','')}{skip_note}</span>
            </div>
            <div style="display:flex;align-items:center;gap:.5rem;flex-shrink:0">
              {tok_note}
              <div class="score-pill" style="background:{_sc(r['pct'])}">
                {r['total_score']}/{r['max_score']} · {r['pct']}%
              </div>
              <span class="chev">▾</span>
            </div>
          </div>
          <div class="turns" style="display:none">{turns_html}</div>
        </div>"""

    type_rows = "".join(
        f"<tr><td>{t}</td><td>{g['count']}</td><td>{g['score']}/{g['max']}</td>"
        f"<td>{_bar(g['pct'],_sc(g['pct']))}</td>"
        f"<td class='mono'>{_tok(g['avg_input_per_q'])}</td>"
        f"<td class='mono'>{_tok(g['avg_output_per_q'])}</td></tr>"
        for t, g in sorted(summary["by_type"].items(), key=lambda x: -x[1]["pct"])
    )

    diff_rows = "".join(
        f"<tr><td>{_chip(d)}</td><td>{g['count']}</td><td>{g['score']}/{g['max']}</td>"
        f"<td>{_bar(g['pct'],_sc(g['pct']))}</td>"
        f"<td class='mono'>{_tok(g['avg_input_per_q'])}</td>"
        f"<td class='mono'>{_tok(g['avg_output_per_q'])}</td></tr>"
        for d, g in summary["by_difficulty"].items()
    )

    total_input  = summary.get("total_input_tokens",  0)
    total_output = summary.get("total_output_tokens", 0)
    total_turns_n = summary.get("total_turns", 1) or 1
    avg_in_q     = round(total_input  / total_turns_n)
    avg_out_q    = round(total_output / total_turns_n)

    cc  = _sc(pct)
    css = f"""
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;600;800&display=swap');
:root{{--bg:#0c0c10;--s:#14141a;--s2:#1a1a22;--b:#252530;--t:#e4e4f0;--m:#5a5a72;
  --green:#22c55e;--yellow:#f59e0b;--red:#ef4444;--blue:#60a5fa;--purple:#a78bfa;--r:10px;
  --mono:'JetBrains Mono',monospace;--sans:'Syne',sans-serif}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--sans);background:var(--bg);color:var(--t);min-height:100vh;padding:2rem 2.5rem}}
h1{{font-size:2.1rem;font-weight:800;letter-spacing:-.04em}}
h1 span{{color:var(--blue)}}
.meta{{font-family:var(--mono);font-size:.72rem;color:var(--m);margin-top:.35rem;margin-bottom:2.5rem}}
.hero{{display:flex;align-items:center;gap:2rem;margin-bottom:3rem;flex-wrap:wrap}}
.ring{{width:130px;height:130px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;
  justify-content:center;background:conic-gradient({cc} {pct*3.6}deg,#1e1e28 0);
  box-shadow:0 0 40px {cc}44}}
.inner{{width:98px;height:98px;border-radius:50%;background:var(--bg);display:flex;
  flex-direction:column;align-items:center;justify-content:center}}
.rpct{{font-size:1.75rem;font-weight:800;color:{cc};font-family:var(--mono)}}
.rlbl{{font-size:.55rem;color:var(--m);text-transform:uppercase;letter-spacing:.12em}}
.hgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1.5fr));gap:.7rem;flex:1}}
.stat{{background:var(--s);border:1px solid var(--b);border-radius:var(--r);padding:.75rem 1rem}}
.sv{{font-size:1.5rem;font-weight:800;font-family:var(--mono)}}
.sl{{font-size:.62rem;color:var(--m);text-transform:uppercase;letter-spacing:.1em;margin-top:.12rem}}
.green{{color:var(--green)}} .yellow{{color:var(--yellow)}} .red{{color:var(--red)}} .blue{{color:var(--blue)}} .purple{{color:var(--purple)}}
.sec{{margin-bottom:2.5rem}}
.stitle{{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.15em;color:var(--m);
  margin-bottom:.8rem;padding-bottom:.45rem;border-bottom:1px solid var(--b)}}
table{{width:100%;border-collapse:collapse;background:var(--s);border-radius:var(--r);overflow:hidden;font-size:.875rem}}
th{{text-align:left;padding:.5rem .9rem;font-size:.62rem;text-transform:uppercase;letter-spacing:.12em;
  color:var(--m);border-bottom:1px solid var(--b);font-weight:700}}
td{{padding:.5rem .9rem;border-bottom:1px solid #171720}}
tr:last-child td{{border-bottom:none}}
td.mono{{font-family:var(--mono);font-size:.76rem;color:var(--m)}}
.bar-wrap{{display:flex;align-items:center;gap:.5rem}}
.bar{{height:7px;border-radius:3px;min-width:2px}}
.bar-wrap span{{font-family:var(--mono);font-size:.76rem;color:var(--m)}}
.fbar{{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.9rem;align-items:center}}
.fl{{font-size:.68rem;color:var(--m);margin-right:.2rem;text-transform:uppercase;letter-spacing:.1em}}
.fbtn{{background:var(--s);border:1px solid var(--b);border-radius:20px;padding:.25rem .8rem;
  font-size:.76rem;cursor:pointer;color:var(--t);font-family:var(--sans);transition:all .15s}}
.fbtn.active{{background:var(--blue);border-color:var(--blue);color:#000;font-weight:700}}
.fbtn:hover:not(.active){{border-color:var(--blue)}}
.cards{{display:flex;flex-direction:column;gap:.55rem}}
.card{{background:var(--s);border:1px solid var(--b);border-radius:var(--r);overflow:hidden;transition:border-color .15s}}
.card:hover{{border-color:#303040}}
.card-header{{display:flex;align-items:center;justify-content:space-between;
  padding:.75rem 1.1rem;gap:1rem;flex-wrap:wrap;cursor:pointer;user-select:none}}
.card-title{{display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;flex:1;min-width:0}}
.ex-id{{font-family:var(--mono);font-size:.78rem;font-weight:700;color:var(--blue);flex-shrink:0}}
.type-tag{{font-size:.62rem;color:var(--m);background:#1a1a22;padding:.1rem .4rem;border-radius:4px;white-space:nowrap}}
.desc{{font-size:.8rem;color:var(--m);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.skip-note{{font-size:.68rem;color:var(--m);opacity:.7}}
.score-pill{{padding:.2rem .65rem;border-radius:20px;font-family:var(--mono);font-size:.76rem;font-weight:700;color:#000;white-space:nowrap}}
.ex-tok{{font-family:var(--mono);font-size:.65rem;color:var(--m);white-space:nowrap;
  background:#0d0d14;border:1px solid #202028;border-radius:6px;padding:.15rem .45rem;flex-shrink:0}}
.chev{{font-size:.78rem;color:var(--m);transition:transform .2s;flex-shrink:0}}
.chev.open{{transform:rotate(180deg)}}
.chip{{padding:.1rem .5rem;border-radius:12px;font-size:.62rem;font-weight:700;flex-shrink:0}}
.badge{{padding:.1rem .42rem;border-radius:4px;font-size:.62rem;font-weight:700;color:#fff;white-space:nowrap}}
.turns{{padding:0 1.1rem 1rem;display:flex;flex-direction:column;gap:.55rem}}
.turn{{background:#0d0d12;border:1px solid #1c1c25;border-radius:8px;overflow:hidden}}
.turn-header{{display:flex;align-items:center;gap:.55rem;padding:.42rem .85rem;background:#09090e;flex-wrap:wrap}}
.turn-num{{font-family:var(--mono);font-size:.7rem;font-weight:700;color:var(--blue)}}
.score-mini{{font-family:var(--mono);font-size:.68rem;color:var(--m)}}
.tok-badge{{font-family:var(--mono);font-size:.63rem;color:#4a7a4a;background:#080f08;
  border:1px solid #131e13;border-radius:4px;padding:.08rem .35rem;white-space:nowrap}}
.tok-badge.tok-out{{color:#7060a0;background:#0a080f;border-color:#1a1425}}
.latency{{font-family:var(--mono);font-size:.66rem;color:var(--m);margin-left:auto}}
.turn-body{{padding:.8rem;display:flex;flex-direction:column;gap:.6rem}}
.turn-cols{{display:grid;grid-template-columns:1fr 1fr;gap:.8rem}}
@media(max-width:660px){{.turn-cols{{grid-template-columns:1fr}}}}
.field label{{display:block;font-size:.58rem;text-transform:uppercase;letter-spacing:.12em;color:var(--m);margin-bottom:.28rem;font-weight:700}}
.field p{{font-size:.83rem;line-height:1.55}}
.field pre{{font-family:var(--mono);font-size:.7rem;white-space:pre-wrap;background:#070710;
  border:1px solid #1c1c25;border-radius:6px;padding:.6rem;line-height:1.5;max-height:200px;overflow-y:auto}}
.reason-box{{font-size:.76rem;color:var(--m);background:#070710;border-radius:6px;padding:.38rem .65rem;border-left:3px solid var(--b)}}
"""

    js = """
function toggle(h){
  const turns=h.nextElementSibling,chev=h.querySelector('.chev'),open=turns.style.display!=='none';
  turns.style.display=open?'none':'flex';
  if(!open){turns.style.flexDirection='column';turns.style.gap='.55rem';}
  chev.classList.toggle('open',!open);
}
function filter(diff,btn){
  document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(c=>{
    c.style.display=(diff==='all'||c.dataset.diff===diff)?'':'none';
  });
}
function filterV(v,btn){
  document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(c=>{
    const has=[...c.querySelectorAll('.badge')].some(b=>b.textContent.includes(v));
    c.style.display=has?'':'none';
  });
}
document.querySelectorAll('.card').forEach(card=>{
  const bad=[...card.querySelectorAll('.badge')].some(b=>
    b.textContent.includes('WRONG')||b.textContent.includes('PARTIAL')||b.textContent.includes('ERROR'));
  if(bad) toggle(card.querySelector('.card-header'));
});
"""

    html = f"""<!DOCTYPE html>
<html lang="fr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PharmaData Agent · Eval</title>
<style>{css}</style></head><body>

<h1>Pharma<span>Data</span> Agent · Eval</h1>
<p class="meta">{summary['run_date'][:19].replace('T',' ')} &nbsp;|&nbsp; {Path(summary['dataset']).name} &nbsp;|&nbsp; {total_ex} exercises</p>

<div class="hero">
  <div class="ring"><div class="inner">
    <span class="rpct">{pct}%</span><span class="rlbl">Overall</span>
  </div></div>
  <div class="hgrid">
    <div class="stat"><div class="sv blue">{summary['total_score']}/{summary['max_score']}</div><div class="sl">Points</div></div>
    <div class="stat"><div class="sv">{total_ex}</div><div class="sl">Exercises</div></div>
    <div class="stat"><div class="sv green">{verdicts.get('CORRECT',0)}</div><div class="sl">Correct</div></div>
    <div class="stat"><div class="sv yellow">{verdicts.get('PARTIAL',0)}</div><div class="sl">Partial</div></div>
    <div class="stat"><div class="sv red">{verdicts.get('WRONG',0)+verdicts.get('AGENT_ERROR',0)}</div><div class="sl">Wrong</div></div>
    <div class="stat"><div class="sv" style="color:var(--m)">{verdicts.get('SKIPPED',0)}</div><div class="sl">Skipped</div></div>
    <div class="stat"><div class="sv purple">{_tok(total_input)}</div><div class="sl">Total token In</div></div>
    <div class="stat"><div class="sv purple">{_tok(total_output)}</div><div class="sl">Total token Out</div></div>
    <div class="stat"><div class="sv" style="color:var(--purple);font-size:1.1rem">{_tok(avg_in_q)}</div><div class="sl">Avg tk In / Q</div></div>
    <div class="stat"><div class="sv" style="color:var(--purple);font-size:1.1rem">{_tok(avg_out_q)}</div><div class="sl">Avg tk Out / Q</div></div>
  </div>
</div>

<div class="sec"><div class="stitle">By Difficulty</div>
  <table><tr><th>Level</th><th>Exercises</th><th>Points</th><th>Score</th><th>Avg tk In / Q</th><th>Avg Out / Q</th></tr>{diff_rows}</table>
</div>

<div class="sec"><div class="stitle">By Type</div>
  <table><tr><th>Type</th><th>Exercises</th><th>Points</th><th>Score</th><th>Avg tk In / Q</th><th>Avg Out / Q</th></tr>{type_rows}</table>
</div>

<div class="sec"><div class="stitle">Exercise Details</div>
  <div class="fbar">
    <span class="fl">Filter:</span>
    <button class="fbtn active" onclick="filter('all',this)">All</button>
    <button class="fbtn" onclick="filter('easy',this)">Easy</button>
    <button class="fbtn" onclick="filter('medium',this)">Medium</button>
    <button class="fbtn" onclick="filter('hard',this)">Hard</button>
    <button class="fbtn" onclick="filterV('WRONG',this)">❌ Wrong</button>
    <button class="fbtn" onclick="filterV('PARTIAL',this)">⚠️ Partial</button>
    <button class="fbtn" onclick="filterV('CORRECT',this)">✅ Correct</button>
    <button class="fbtn" onclick="filterV('SKIPPED',this)">⏭ Skipped</button>
  </div>
  <div class="cards">{cards_html}</div>
</div>

<script>{js}</script>
</body></html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


async def _main():
    parser = argparse.ArgumentParser(description="PharmaData Agent — Evaluation Pipeline")
    parser.add_argument("--dataset", default="pharmatech_test_dataset.json")
    parser.add_argument("--output",  default="results")
    parser.add_argument("--excel",  default="pharmatech_data.xlsx")
    parser.add_argument("--ids",     default=None,
                        help="Comma-separated exercise IDs (e.g. EX01,EX15,EX41)")
    args = parser.parse_args()
    filter_ids = [i.strip() for i in args.ids.split(",")] if args.ids else None
    
    with tempfile.NamedTemporaryFile(
      prefix="eval_pipeline_",
      suffix=".xlsx",
      dir=tempfile.gettempdir(),
    ) as tmp:
      filepath = tmp.name
      with open(args.excel, "rb") as src:
          shutil.copyfileobj(src, tmp)
      
      await run_pipeline(dataset_path=args.dataset, excel_path=filepath, output_dir=args.output, filter_ids=filter_ids)


if __name__ == "__main__":
    asyncio.run(_main())