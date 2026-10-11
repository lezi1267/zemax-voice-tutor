# -*- coding: utf-8 -*-
"""学生拖一个文件进来 → 抽文本 → 切块存起来 → 教课时检索引用。

存:voice-tutor\\knowledge\\<名字>.json(块 + 关键词 + 向量)。
检索:词重叠 × 逆文档频率(jieba 是 funasr 带的)+ 智谱 embedding-2 余弦相似度,两路合分。
向量拿不到(断网/额度)就自动退回纯词袋,提问不会炸。
收件箱:voice-tutor\\knowledge\\收件箱\\ —— 把文件拖进去,下次提问自动学会。

ponytail: 每次提问把所有块算一遍余弦,O(总块数),块数上万再考虑 ANN 索引。
"""
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from pipecat.adapters.schemas.function_schema import FunctionSchema

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

EMBED_MODEL = os.getenv("GLM_EMBED_MODEL", "embedding-2")
_EMBED_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
DIR = Path(__file__).resolve().parent / "knowledge"
INBOX = DIR / "收件箱"
CHUNK = 700  # 一块约 700 字:够一段讲解,又不会把无关内容一起塞给模型
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".py", ".log", ".yaml", ".yml"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def _embed(texts: list) -> list | None:
    """文字 → 向量(一次最多 16 条,多了分批)。拿不到就返回 None,检索退回纯词袋。"""
    import requests

    key = os.getenv("GLM_API_KEY")
    if not key or not texts:
        return None
    out = []
    for i in range(0, len(texts), 16):
        try:
            r = requests.post(_EMBED_URL, timeout=45,
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": EMBED_MODEL, "input": texts[i:i + 16]})
            r.raise_for_status()
            rows = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            for row in rows:
                out.append([round(float(v), 4) for v in row["embedding"]])
        except Exception:
            return None  # 额度/网络问题:纯词袋照样能用
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^\w一-鿿-]+", "_", s).strip("_")[:60] or "doc"


def _terms(text: str) -> list:
    import jieba

    return sorted({w.lower() for w in jieba.cut(text) if len(w) >= 2 and w.strip()})


def extract(path: Path) -> str:
    """各种文件 → 纯文本。不支持的类型如实报错,不硬编。"""
    suf = path.suffix.lower()
    if suf in TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="replace")
    if suf == ".docx":
        import zipfile

        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))
    if suf == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("读 PDF 要 pypdf:在 voice-tutor 目录跑 .venv\\Scripts\\pip install pypdf")
        reader = PdfReader(str(path))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        if not text.strip():
            raise RuntimeError(f"这份 PDF({len(reader.pages)}页)抽不出文字:是扫描件。"
                              "把关键页截图拖进来,我改用眼睛看")
        return text
    if suf in IMG_EXT:
        import vision

        return vision.ask_file(path, "把这张图的内容读出来:标题、坐标轴、曲线形状、表格和所有数值。")
    raise ValueError(f"不支持的文件类型 {suf or '(无扩展名)'}:支持 文本/md/docx/pdf/图片")


def _chunks(text: str) -> list:
    """按空行分段,攒成约 CHUNK 字的块(段落不会被腰斩)。"""
    parts, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) > CHUNK:
            parts.append(cur)
            cur = para
        else:
            cur = (cur + "\n" + para).strip()
    if cur:
        parts.append(cur)
    return parts


def _headings(text: str) -> list:
    """挑几行像标题的,给模型一份"这份资料讲了什么"的骨架。"""
    out = []
    for line in text.splitlines()[:400]:
        s = line.strip().strip("#* ").rstrip("。,.:：")
        if 4 <= len(s) <= 40 and not re.search(r"\d\.\d", s) and s not in out:
            out.append(s)
        if len(out) >= 12:
            break
    return out


def _docs() -> list:
    out = []
    for f in sorted(DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def learn(path) -> dict:
    """学一份文件:抽文本 → 切块 → 存 knowledge\\。同一个文件没改过就不重学。"""
    p = Path(str(path).strip().strip('"')).expanduser()
    if not p.is_file():
        return {"error": f"文件不存在:{p}"}
    DIR.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    mtime = p.stat().st_mtime
    doc_path = DIR / (_slug(p.stem) + ".json")
    if doc_path.exists():
        old = json.loads(doc_path.read_text(encoding="utf-8"))
        if old.get("path") == str(p) and old.get("mtime") == mtime:
            return {"ok": True, "已学过": old["name"], "提示": "文件没变,直接 ask_knowledge 问它"}
    try:
        text = extract(p)
    except Exception as e:
        return {"error": f"{p.name} 读不了:{e}"}
    chunks = _chunks(text)
    if not chunks:
        return {"error": f"{p.name} 里没抽出内容"}
    freq: dict[str, int] = {}
    stored = []
    for c in chunks:
        t = _terms(c)
        stored.append({"terms": t, "text": c})
        for w in t:
            freq[w] = freq.get(w, 0) + 1
    vecs = _embed([c["text"] for c in stored])  # 学一次存下来,以后提问只嵌一句话
    if vecs and len(vecs) == len(stored):
        for c, v in zip(stored, vecs):
            c["vec"] = v
    doc = {"name": p.stem, "path": str(p), "mtime": mtime,
           "learned": time.strftime("%Y-%m-%d %H:%M"), "chars": len(text),
           "keywords": [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:12]],
           "outline": _headings(text), "chunks": stored}
    tmp = doc_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, doc_path)
    return {"ok": True, "学了": doc["name"], "字数": doc["chars"], "知识块": len(stored),
            "关键词": doc["keywords"], "目录": doc["outline"],
            "向量": "已存" if any("vec" in c for c in stored) else "没存上(检索退回纯词袋)",
            "提示": "要引用它回答问题时调 ask_knowledge(学生的问题)"}


def scan_inbox() -> list:
    """收件箱里新拖进来的文件,挨个学。提问时顺手扫一次,不用重启程序。"""
    if not INBOX.is_dir():
        return []
    known = {d.get("path") for d in _docs()}
    done = []
    for f in sorted(INBOX.iterdir()):
        if f.is_file() and not f.name.startswith(".") and str(f) not in known:
            r = learn(f)
            done.append({"文件": f.name, **r})
    return done


def query(question: str, k: int = 3) -> dict:
    """两路合分:词重叠×逆文档频率 + 向量余弦(各归一化到 0~1,各占一半)。向量拿不到就纯词袋。"""
    docs = _docs()
    if not docs:
        return {"error": "还没学过任何资料:让学生把文件拖进 voice-tutor\\knowledge\\收件箱\\,或给我路径"}
    q = set(_terms(question))
    qv = _embed([question])
    qv = qv[0] if qv and len(qv) == 1 else None
    if not q and not qv:
        return {"error": "问题里没认出可检索的词,换个说法"}
    df: dict[str, int] = {}
    for d in docs:
        for c in d["chunks"]:
            for w in c["terms"]:
                df[w] = df.get(w, 0) + 1
    cand = []
    for d in docs:
        for i, c in enumerate(d["chunks"]):
            kw = sum(1.0 / (1.0 + df.get(w, 1)) for w in q & set(c["terms"]))
            cos = _cos(qv, c.get("vec")) if qv else 0.0
            if kw or (qv and c.get("vec")):
                cand.append([kw, cos, d["name"], i, c["text"]])
    if not cand:
        return {"命中": [], "说明": "学过的资料里没有这个词,如实告诉学生,别编"}
    # 余弦有基线(不相关的中文段落也有 0.3 左右),按本次候选的分布归一化才分得开
    cos_lo, cos_hi = min(c[1] for c in cand), max(c[1] for c in cand)
    kw_max = max(c[0] for c in cand)
    for c in cand:
        c.append(0.5 * c[0] / (kw_max or 1.0)
                 + 0.5 * ((c[1] - cos_lo) / (cos_hi - cos_lo) if cos_hi - cos_lo > 1e-3 else 0.0))
    cand.sort(key=lambda x: -x[-1])
    out = {"命中": [{"来源": name, "块": i, "相关度": round(score, 2),
                     "词分": round(kw, 2), "向量分": round(cos, 2), "原文": text[:1200]}
                    for kw, cos, name, i, text, score in cand[:k]],
            "检索": "词 + 向量" if qv else "纯词(向量不可用)",
            "提示": "按原文回答,并告诉学生是出自《来源》这份资料"}
    if kw_max == 0 and cos_hi < 0.42:
        # 实测 embedding-2:相关的块 0.53 上下,不相关的 0.34 上下,0.42 以下基本是没讲到
        out["提醒"] = f"相关度很低(词没命中,语义只有 {cos_hi:.2f}):这份资料多半没讲到,如实告诉学生,别硬引"
    return out


def _cos(a, b) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = sum(v * v for v in a[:n]) ** 0.5
    nb = sum(v * v for v in b[:n]) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


async def _learn_file_handler(params) -> None:
    args = params.arguments or {}
    result = await _run(learn, args.get("path", ""))
    await params.result_callback(result)


async def _list_knowledge_handler(params) -> None:
    new = await _run(scan_inbox)
    docs = _docs()
    await params.result_callback({
        "新学的": new or "收件箱里没有新文件",
        "已学资料": [{"名称": d["name"], "字数": d["chars"], "关键词": d["keywords"][:8]} for d in docs]
        or "还没有",
        "收件箱": str(INBOX),
    })


async def _ask_knowledge_handler(params) -> None:
    await _run(scan_inbox)
    args = params.arguments or {}
    result = await _run(query, args.get("question", ""), int(args.get("top", 3) or 3))
    await params.result_callback(result)


async def _run(fn, *a):
    """抽文本/jieba 分词是阻塞的,丢 worker 线程,别冻住语音管线。"""
    import asyncio

    return await asyncio.to_thread(fn, *a)


learn_file_schema = FunctionSchema(
    name="learn_file",
    description=(
        "学习一份资料:学生拖文件进对话小窗、说'我拖进收件箱了'、或给你一个文件路径(如课件、"
        "Zemax 手册章节、教材 PDF、截图)时调用,path 填完整路径。学会后就能按它讲课。"
    ),
    properties={"path": {"type": "string", "description": "文件完整路径"}},
    required=["path"],
    handler=_learn_file_handler,
)

list_knowledge_schema = FunctionSchema(
    name="list_knowledge",
    description="看学过哪些资料,顺便把收件箱里新拖进来的文件学掉。学生问'你学过什么资料''我拖的文件学会了吗'时调用。",
    properties={},
    required=[],
    handler=_list_knowledge_handler,
)

ask_knowledge_schema = FunctionSchema(
    name="ask_knowledge",
    description=(
        "在学过的资料里检索:学生问'你给我的那份资料里怎么说…''按课件第X节…',或你讲课要引原文时调用。"
        "返回最相关的原文块和来源;没命中就如实说,别编。"
    ),
    properties={
        "question": {"type": "string", "description": "要查的问题或关键词"},
        "top": {"type": "integer", "description": "返回几块,默认 3"},
    },
    required=["question"],
    handler=_ask_knowledge_handler,
)

TOOLS = [learn_file_schema, list_knowledge_schema, ask_knowledge_schema]


if __name__ == "__main__":
    # 自检:造一份 docx + 一份 md,学完检索,断言命中。
    # 词那一路不联网;向量那一路要联网(拿不到 key 就跳过那几条断言),都不碰真资料。
    import shutil
    import tempfile
    import zipfile

    tmp = Path(tempfile.mkdtemp())
    real_dir = DIR
    DIR = tmp / "knowledge"
    INBOX = DIR / "收件箱"
    try:
        docx = tmp / "消色差入门.docx"
        with zipfile.ZipFile(docx, "w") as z:
            z.writestr("word/document.xml",
                       "<w:document><w:body><w:p><w:r><w:t>消色差双胶合透镜</w:t></w:r></w:p>"
                       "<w:p><w:r><w:t>用冕牌玻璃和火石玻璃配对,阿贝数一高一低,把轴向色差压到接近零。</w:t></w:r></w:p>"
                       "</w:body></w:document>")
        md = tmp / "牛顿望远镜.md"
        md.write_text("# 牛顿望远镜\n\n反射镜没有色差,主镜是抛物面。\n\n副镜是平面,45 度折光。\n",
                      encoding="utf-8")
        r1 = learn(docx)
        r2 = learn(md)
        assert r1.get("ok") and r1["字数"] > 20, r1
        assert r2.get("ok") and r2["知识块"] >= 1, r2
        assert learn(docx).get("已学过"), "同一文件应识别成已学过"

        hit = query("火石玻璃配对压色差")
        assert hit["命中"] and hit["命中"][0]["来源"] == "消色差入门", hit
        assert "消色差双胶合透镜" in hit["命中"][0]["原文"]

        # 向量那一路:纯词袋抓不到的语义问题靠它;词和语义都没命中时要如实喊"没讲到"
        if hit.get("检索") == "词 + 向量":
            sem = query("主镜是什么形状")
            assert sem["命中"][0]["来源"] == "牛顿望远镜", sem
            para = query("为什么两种介质粘在一起就能解决颜色散开的毛病")  # 和资料一个词都不重叠
            assert para["命中"][0]["来源"] == "消色差入门", para
            weak = query("相对照度渐晕")
            assert weak["命中"][0]["词分"] == 0 and "提醒" in weak, weak
        else:
            print("向量不可用,只测了词检索(纯词袋兜底路径)")
            assert query("相对照度渐晕").get("命中") == []

        INBOX.mkdir(parents=True, exist_ok=True)
        shutil.copy(md, INBOX / "副本.md")
        (DIR / "副本.json").unlink(missing_ok=True)
        new = scan_inbox()
        assert any(n.get("ok") for n in new), new

        print(f"knowledge 自检通过[{hit.get('检索')}]:", r1["关键词"], "|", hit["命中"][0]["来源"])
    finally:
        DIR, INBOX = real_dir, real_dir / "收件箱"
        shutil.rmtree(tmp, ignore_errors=True)
