"""Build a portable demo replay from archived, synthetic acceptance outputs."""

import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    report = json.loads((ROOT / "eval/results/acceptance_v1_first_run.json").read_text())
    cases = {case["id"]: case for case in report["cases"]}
    selected = [("SQL analysis", cases["running_monthly_total"]["turns"][0]),
                ("Revenue chart", cases["country_chart"]["turns"][0]),
                ("A/B comparison", cases["missing_counts_followup"]["turns"][1])]
    frames = []
    data = []
    font_path = next((p for p in [Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")] if p.exists()), None)
    font = lambda n: ImageFont.truetype(str(font_path), n) if font_path else ImageFont.load_default()
    for index, (title, turn) in enumerate(selected):
        entries = [e for e in turn["workflow"]["tool_results"] if e["status"] == "success" and e.get("evidence")]
        evidence = entries[-1]["evidence"]
        metrics = turn["metrics"]
        item = {"title": title, "question": turn["question"], "answer": turn["answer"],
                "columns": evidence.get("columns", []), "rows": evidence.get("rows", []),
                "stats": evidence.get("stats"), "duration": round(turn["duration_ms"] / 1000, 2),
                "model_calls": metrics["model_calls"], "tool_calls": metrics["tool_calls"],
                "tokens": metrics["token_usage"]["total_tokens"]}
        if evidence["kind"] == "chart":
            artifact = turn["artifacts"][0]
            source = ROOT / artifact["path"]
            if source.is_file():
                shutil.copyfile(source, HERE / "country_revenue.png")
            elif not (HERE / "country_revenue.png").is_file():
                raise FileNotFoundError("Archived chart is required for demo generation.")
            item["image"] = "country_revenue.png"
        data.append(item)
        image = Image.new("RGB", (1400, 880), "#eef2f8")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((35, 30, 1365, 850), radius=25, fill="white")
        draw.text((75, 60), "AI Analytics Agent 2.0", font=font(35), fill="#17233b")
        draw.text((75, 110), "RECORDED SYNTHETIC OUTPUTS  /  SQL  >  CHART  >  A/B", font=font(18), fill="#56657b")
        draw.rounded_rectangle((75, 155, 1325, 222), radius=15, fill="#e8eefe")
        draw.text((100, 173), f"{index + 1} / 3   {title}", font=font(28), fill="#2848a5")
        if item.get("image"):
            chart = Image.open(HERE / item["image"]).convert("RGB")
            chart.thumbnail((1100, 535))
            image.paste(chart, ((1400 - chart.width) // 2, 235))
        elif item["stats"]:
            stats = item["stats"]
            lines = [f"Control conversion:      {stats['control_conversion_rate']:.0%}",
                     f"Treatment conversion:    {stats['treatment_conversion_rate']:.0%}",
                     f"Absolute lift:           {stats['absolute_lift'] * 100:.0f} percentage points",
                     f"Two-sided p-value:       {stats['p_value']:.5f}",
                     "Evidence supports higher treatment conversion.",
                     "Review experiment validity and practical value before rollout."]
            for line_index, line in enumerate(lines):
                draw.text((105, 270 + line_index * 65), line, font=font(27), fill="#23334f")
        else:
            widths = [410, 365, 365]
            headers = ["Month", "Monthly net revenue", "Cumulative revenue"]
            x = 105
            for header, width in zip(headers, widths):
                draw.text((x, 270), header, font=font(23), fill="#56657b")
                x += width
            for row_index, row in enumerate(item["rows"]):
                x = 105
                for column, width in zip(item["columns"], widths):
                    value = row[column]
                    text = f"{value:g}" if isinstance(value, (int, float)) else str(value)
                    draw.text((x, 335 + row_index * 65), text, font=font(27), fill="#23334f")
                    x += width
        draw.text((80, 795), f"Captured runtime: {item['duration']}s   |   {item['model_calls']} model calls   |   {item['tool_calls']} tool calls",
                  font=font(20), fill="#56657b")
        frames.append(image)
    frames[0].save(HERE / "preview.gif", save_all=True, append_images=frames[1:], duration=6500, loop=0, optimize=True)
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Analytics Agent — Recorded Demo</title><style>
*{box-sizing:border-box}body{margin:0;background:#eef2f8;color:#17233b;font:16px/1.6 system-ui,sans-serif}main{max-width:1200px;margin:50px auto;padding:0 24px}.eyebrow{font-size:12px;letter-spacing:2px;color:#58677c}h1{font-size:36px;margin:8px 0}header p{color:#58677c;max-width:850px}.badge{display:inline-block;background:#e2eafb;color:#2848a5;border-radius:20px;padding:5px 14px;font-size:13px}.layout{display:grid;grid-template-columns:340px 1fr;gap:24px;margin:30px 0}.card{background:white;border:1px solid #dce3ee;border-radius:18px;padding:25px;box-shadow:0 8px 30px #21314c06}h2{font-size:23px;margin-top:0}.question{font-size:17px}.metric{display:flex;justify-content:space-between;border-top:1px solid #edf0f5;padding:10px 0;color:#58677c}.metric strong{color:#17233b}.content img{width:100%;height:auto}table{border-collapse:collapse;width:100%;font-size:15px}td,th{padding:13px 10px;border-bottom:1px solid #e6ebf2;text-align:left}th{color:#58677c;font-weight:500}.answer{white-space:pre-wrap;font-size:14px;max-height:380px;overflow:auto}.ab{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:22px}.ab div{border-radius:14px;background:#f3f6fb;padding:25px}.ab strong{display:block;font-size:42px}button{font:inherit;border-radius:10px;border:1px solid #ccd6e6;background:white;padding:9px 20px;cursor:pointer}button.primary{background:#2848a5;color:white;border-color:#2848a5}nav{display:flex;align-items:center;gap:12px}nav span{margin-left:auto;color:#58677c}footer{font-size:13px;color:#68758b;margin:24px 0}a{color:#2848a5}@media(max-width:800px){main{margin-top:24px}.layout{grid-template-columns:1fr}h1{font-size:28px}}
</style><main><header><div class="eyebrow">AI ANALYTICS AGENT 2.0</div><h1>From a question to verified evidence</h1><p>Three actual recorded scenarios: SQL analysis, a revenue chart, and an A/B comparison. All data is invented. This replay makes no live model calls.</p><span class="badge">New acceptance suite · 19 scenarios · 25 turns checked and reviewed</span></header>
<div class="layout"><section class="card"><h2 id="title"></h2><p class="question" id="question"></p><div class="metric">Captured runtime<strong id="duration"></strong></div><div class="metric">Model calls<strong id="models"></strong></div><div class="metric">Tool calls<strong id="tools"></strong></div><div class="metric">Reported tokens<strong id="tokens"></strong></div></section><section class="card content"><div id="visual"></div><details><summary>Read the recorded answer</summary><p class="answer" id="answer"></p></details></section></div>
<nav><button id="back">Previous</button><button class="primary" id="play">Play replay</button><button id="next">Next</button><span id="step"></span></nav><footer>Recorded with the configured GPT-5.6 model. References were frozen before the live run. Answer review: Codex, not an independent human study. This is an output replay, not a screen recording. <a href="../acceptance.md">Evaluation notes</a></footer></main>
<script id="data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('data').textContent);let index=0,timer=null;const el=id=>document.getElementById(id);function cell(tag,text){const e=document.createElement(tag);e.textContent=text;return e}function render(){const d=data[index];el('title').textContent=d.title;el('question').textContent=d.question;el('duration').textContent=d.duration+' s';el('models').textContent=d.model_calls;el('tools').textContent=d.tool_calls;el('tokens').textContent=d.tokens.toLocaleString();el('answer').textContent=d.answer;el('step').textContent=(index+1)+' / '+data.length;const box=el('visual');box.replaceChildren();if(d.image){const img=document.createElement('img');img.src=d.image;img.alt='Actual country net-revenue chart from the recorded tool output';box.append(img)}else if(d.stats){const rates=document.createElement('div');rates.className='ab';for(const [label,key]of [['Control','control_conversion_rate'],['Treatment','treatment_conversion_rate']]){const item=document.createElement('div');item.append(cell('span',label),cell('strong',(d.stats[key]*100).toFixed(0)+'%'));rates.append(item)}box.append(rates,cell('p','Absolute lift: '+(d.stats.absolute_lift*100).toFixed(0)+' percentage points. Two-sided p-value: '+d.stats.p_value.toFixed(5)+'.'),cell('p','The treatment shows a statistically significant improvement. Review experiment validity and practical value before rollout.'))}else{const table=document.createElement('table'),head=document.createElement('tr');for(const c of d.columns)head.append(cell('th',c.replaceAll('_',' ')));table.append(head);for(const row of d.rows){const tr=document.createElement('tr');for(const c of d.columns)tr.append(cell('td',row[c]));table.append(tr)}box.append(table)}}function stop(){clearInterval(timer);timer=null;el('play').textContent='Play replay'}el('back').onclick=()=>{stop();index=(index+data.length-1)%data.length;render()};el('next').onclick=()=>{stop();index=(index+1)%data.length;render()};el('play').onclick=()=>{if(timer)return stop();el('play').textContent='Pause replay';timer=setInterval(()=>{index=(index+1)%data.length;render()},6500)};render();
</script></html>'''
    (HERE / "index.html").write_text(page.replace("PAYLOAD", payload))
    print(json.dumps({"demo": str(HERE / "index.html"), "preview": str(HERE / "preview.gif")}))


if __name__ == "__main__":
    main()
