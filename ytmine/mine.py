#!/usr/bin/env python3
"""
유튜브 댓글에서 '마찰'을 캐내는 도구.

감성(긍정/부정)이 아니라 발화 유형으로 가른다. 사람들이 자기 사정을 털어놓은
문장이 곧 문제 정의이기 때문이다.

  좋아요 = 합의 (같은 문제를 가진 사람이 몇인가)
  대댓글 = 논쟁 (의견이 갈리는 곳인가)

이 둘은 다른 신호라서 한 축으로 뭉개지 않는다.

  python mine.py collect @채널핸들 [@채널2 ...] --videos 30
  python mine.py report --out dashboard.html
"""
import argparse, json, math, os, re, sqlite3, sys, time
from collections import Counter, defaultdict
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError

API = "https://www.googleapis.com/youtube/v3/"
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comments.db")

# 유닛 비용 (참고용 추적)
COST = {"channels": 1, "playlistItems": 1, "videos": 1, "commentThreads": 1, "search": 100}
_spent = 0


def key():
    k = os.environ.get("YT_API_KEY")
    if not k:
        sys.exit("YT_API_KEY 환경변수가 없습니다.  export YT_API_KEY='...'")
    return k


def call(endpoint, **params):
    """API 호출 + 할당량 누적. 429/403(quota)면 중단하고 재개 방법을 안내."""
    global _spent
    params["key"] = key()
    url = API + endpoint + "?" + urlencode(params)
    for attempt in range(4):
        try:
            with urlopen(url, timeout=30) as r:
                _spent += COST.get(endpoint, 1)
                return json.load(r)
        except HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (403, 429) and ("quota" in body.lower() or "rateLimit" in body):
                if "quotaExceeded" in body:
                    print(f"\n[중단] 오늘 할당량을 다 썼습니다. 지금까지 쓴 유닛: 약 {_spent}")
                    print("       한국시간 오후 4시경 리셋됩니다. 같은 명령을 다시 돌리면")
                    print("       이미 받은 댓글은 건너뛰고 이어서 받습니다.")
                    raise SystemExit(1)
                time.sleep(2 ** attempt)
                continue
            if e.code == 403 and "commentsDisabled" in body:
                return {"_disabled": True}
            if e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(f"API 오류 {e.code}: {body[:400]}")
    raise SystemExit("재시도 후에도 실패했습니다.")


def iso_dur(s):
    """PT1H2M3S -> 초. 감정 지도의 가로축이 되므로 영상 길이가 필요하다."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS channel(id TEXT PRIMARY KEY, title TEXT, handle TEXT, subs INTEGER);
    CREATE TABLE IF NOT EXISTS video(
      id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, published TEXT,
      views INTEGER, comments INTEGER, dur INTEGER DEFAULT 0, done INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS comment(
      id TEXT PRIMARY KEY, video_id TEXT, text TEXT, author TEXT,
      likes INTEGER, replies INTEGER, published TEXT);
    CREATE INDEX IF NOT EXISTS ix_c_video ON comment(video_id);
    """)
    return c


# ---------------------------------------------------------------- collect
def resolve_channel(handle):
    h = handle.lstrip("@")
    r = call("channels", part="snippet,contentDetails,statistics", forHandle="@" + h)
    if not r.get("items"):
        r = call("channels", part="snippet,contentDetails,statistics", forUsername=h)
    if not r.get("items"):
        sys.exit(f"채널을 못 찾았습니다: {handle}\n"
                 f"  채널 페이지 URL의 @핸들을 정확히 넣어주세요. 예: @{h}\n"
                 f"  또는 UC로 시작하는 채널ID를 직접 넣어도 됩니다.")
    it = r["items"][0]
    return {
        "id": it["id"],
        "title": it["snippet"]["title"],
        "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"],
        "subs": int(it["statistics"].get("subscriberCount", 0) or 0),
    }


def collect_videos(ids):
    """영상 URL/ID 여러 개를 직접 긁는다. 아웃라이어 한 편만 볼 때 쓴다."""
    con = db()
    ids = [vid_id(x) for x in ids]
    for i in range(0, len(ids), 50):
        r = call("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
        for it in r.get("items", []):
            st = it.get("statistics", {})
            con.execute(
                "INSERT OR IGNORE INTO video(id,channel_id,title,published,views,comments,dur) "
                "VALUES(?,?,?,?,?,?,?)",
                (it["id"], it["snippet"]["channelId"], it["snippet"]["title"],
                 it["snippet"]["publishedAt"], int(st.get("viewCount", 0) or 0),
                 int(st.get("commentCount", 0) or 0),
                 iso_dur(it.get("contentDetails", {}).get("duration", ""))))
            con.execute("INSERT OR IGNORE INTO channel VALUES(?,?,?,0)",
                        (it["snippet"]["channelId"], it["snippet"].get("channelTitle", ""), ""))
    con.commit()
    missing = [v for v in ids if not con.execute(
        "SELECT 1 FROM video WHERE id=?", (v,)).fetchone()]
    if missing:
        print(f"[경고] 찾지 못한 영상: {', '.join(missing)}")
    for vid, title, ncom in con.execute(
            "SELECT id,title,comments FROM video WHERE id IN (%s) AND done=0"
            % ",".join("?" * len(ids)), ids).fetchall():
        got = fetch_comments(con, vid)
        print(f"   {title[:44]:<46} 댓글 {got:>5}   (누적 {_spent}유닛)")
    total = con.execute("SELECT COUNT(*) FROM comment").fetchone()[0]
    print(f"\n완료. 저장된 댓글 {total:,}개 · 이번에 쓴 유닛 약 {_spent} (하루 한도 10,000)")


def fetch_comments(con, vid, cap=2000):
    """한 영상의 댓글을 페이지 단위로 받아 쌓는다."""
    got, tok = 0, None
    while True:
        r = call("commentThreads", part="snippet", videoId=vid, maxResults=100,
                 order="relevance", textFormat="plainText",
                 **({"pageToken": tok} if tok else {}))
        if r.get("_disabled"):
            break
        for th in r.get("items", []):
            sn = th["snippet"]["topLevelComment"]["snippet"]
            con.execute("INSERT OR REPLACE INTO comment VALUES(?,?,?,?,?,?,?)",
                        (th["id"], vid, sn.get("textDisplay", ""),
                         sn.get("authorDisplayName", ""),
                         int(sn.get("likeCount", 0) or 0),
                         int(th["snippet"].get("totalReplyCount", 0) or 0),
                         sn.get("publishedAt", "")))
            got += 1
        tok = r.get("nextPageToken")
        if not tok or got >= cap:
            break
    con.execute("UPDATE video SET done=1 WHERE id=?", (vid,))
    con.commit()
    return got


def collect(handles, n_videos):
    con = db()
    for handle in handles:
        ch = resolve_channel(handle) if not handle.startswith("UC") else None
        if ch is None:
            r = call("channels", part="snippet,contentDetails,statistics", id=handle)
            it = r["items"][0]
            ch = {"id": it["id"], "title": it["snippet"]["title"],
                  "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"],
                  "subs": int(it["statistics"].get("subscriberCount", 0) or 0)}
        print(f"\n■ {ch['title']}  (구독 {ch['subs']:,})")
        con.execute("INSERT OR REPLACE INTO channel VALUES(?,?,?,?)",
                    (ch["id"], ch["title"], handle, ch["subs"]))

        # 업로드 플레이리스트에서 영상 목록 (search.list는 100유닛이라 쓰지 않는다)
        vids, tok = [], None
        while len(vids) < n_videos:
            r = call("playlistItems", part="contentDetails", playlistId=ch["uploads"],
                     maxResults=min(50, n_videos - len(vids)), **({"pageToken": tok} if tok else {}))
            vids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
            tok = r.get("nextPageToken")
            if not tok:
                break

        for i in range(0, len(vids), 50):
            r = call("videos", part="snippet,statistics,contentDetails", id=",".join(vids[i:i + 50]))
            for it in r.get("items", []):
                st = it.get("statistics", {})
                con.execute(
                    "INSERT OR IGNORE INTO video(id,channel_id,title,published,views,comments,dur) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (it["id"], ch["id"], it["snippet"]["title"], it["snippet"]["publishedAt"],
                     int(st.get("viewCount", 0) or 0), int(st.get("commentCount", 0) or 0),
                     iso_dur(it.get("contentDetails", {}).get("duration", ""))))
        con.commit()

        todo = con.execute(
            "SELECT id,title,comments FROM video WHERE channel_id=? AND done=0 "
            "ORDER BY comments DESC", (ch["id"],)).fetchall()
        for vid, title, ncom in todo:
            got = fetch_comments(con, vid, cap=500)
            print(f"   {title[:44]:<46} 댓글 {got:>5}   (누적 {_spent}유닛)")
    total = con.execute("SELECT COUNT(*) FROM comment").fetchone()[0]
    print(f"\n완료. 저장된 댓글 {total:,}개 · 이번에 쓴 유닛 약 {_spent} (하루 한도 10,000)")


# ---------------------------------------------------------------- 분류
# 감성이 아니라 '발화 유형'으로 가른다. 긍정/부정은 행동을 못 만든다.
# "영상 잘 봤어요! 근데 저는 3교대라..." 는 감성분석에선 긍정으로 묻히지만
# 여기서는 상황고백으로 잡힌다. 알맹이는 뒷문장에 있다.
TYPES = [
    ("fail",      "실패·중단 경험", "기존 해법이 깨지는 지점. 만들 것이 여기 있다."),
    ("situation", "자기 상황 고백", "날것의 문제 진술. 가장 값진 재료."),
    ("question",  "미응답 질문",    "영상이 답 안 한 것 = 빈자리."),
    ("request",   "요청",           "바로 만들 수 있는 것."),
    ("alt",       "대안 언급",      "경쟁 상황과 그 불만."),
    ("react",     "단순 반응",      "버려도 되는 것."),
]
CORE = {"fail", "situation"}

RX = {
    "fail": re.compile(r"포기|실패|안\s?되|안됨|못\s?하|안\s?됐|요요|다시\s?쪘|다시\s?찜|"
                       r"작심삼일|중도|그만뒀|그만둠|무너졌|며칠\s?만에|작심"),
    "situation": re.compile(r"(저는|제가|전\s|저도|나는|내가|제\s?경우)|"
                            r"교대|야근|육아|출산|수유|모유|갱년기|허리|무릎|디스크|"
                            r"갑상선|당뇨|호르몬|다낭성|장염|위염|수술|재활|알바|수험"),
    "question": re.compile(r"\?|어떻게|어떡|뭐가|무엇|언제|얼마나|왜\s|어디|몇\s?|"
                           r"되나요|하나요|인가요|일까요|될까요|괜찮나요|맞나요|있나요"),
    "request": re.compile(r"해주세요|알려주세요|올려주|만들어주|부탁|다뤄주|리뷰해|영상\s?부탁"),
    "alt": re.compile(r"(앱|어플|프로그램|제품|보조제|영양제|채널|유튜브|클래스)"
                      r".{0,12}(써봤|해봤|먹어봤|봤는데|썼는데|쓰는데|사용|결제)"),
}

# 내용이 아니라 '영상 만듦새'에 대한 마찰. 같은 주제를 더 잘 전달할 여지가 여기 있다.
RX_FORMAT = re.compile(r"너무\s?길|길어|짧게|결론부터|본론|늘어지|지루|반복되는|"
                       r"배경음|브금|BGM|소리가|음량|목소리|발음|자막|화질|편집|"
                       r"빨리\s?감|스킵|넘겨|요약본|정리본")
SHORT_PRAISE = re.compile(r"^.{0,18}(감사|최고|화이팅|파이팅|응원|잘\s?봤|좋아요|굿|대박|👍|❤)")


def classify(text):
    """주 유형 하나 + 겹치는 신호 태그. 한 댓글이 여러 신호를 동시에 낼 수 있다 —
    "앱 써봤는데 그만뒀어요"는 실패이자 대안 언급이다. 주 유형만 남기면 후자가 증발한다."""
    t = (text or "").strip()
    tags = [k for k in ("fail", "situation", "question", "request", "alt") if RX[k].search(t)]
    if SHORT_PRAISE.match(t) and len(t) < 25:
        return "react", tags
    return (tags[0] if tags else "react"), tags


# 형태소 분석기 없이 쓰는 거친 어미 제거. 완벽하지 않아 키워드는 참고용이다.
ENDING = re.compile(r"(는데|은데|ㄴ데|라서|이라|어서|아서|해서|으면|면서|지만|니까|으니|"
                    r"네요|어요|아요|에요|예요|까요|나요|인가|일까|거나|든지|"
                    r"하는|되는|있는|없는|같은|하고|되고|이고)$")
JOSA = re.compile(r"(은|는|이|가|을|를|에서|에게|에|의|로|으로|도|만|과|와|랑|부터|까지|"
                  r"처럼|보다|이나|엔|나|요)$")
# 내용 없이 문법만 나르는 토큰
GRAM = re.compile(r"^(이런|저런|그런|어떤|무슨|이거|그거|저거|여기|거기|저기|"
                  r"있|없|하|되|같|많|적|좋|나쁘|보|주|받|들|것|수|때|건|게|걸|더|덜)$")
STOP = set("""그리고 그런데 그래서 하지만 정말 진짜 너무 조금 많이 계속 다시 지금 요즘 오늘
내일 어제 이거 그거 저거 이번 저번 다음 하나 그냥 같이 대한 위해 통해 대해 관련 정도
매번 번째 다섯 여섯 일곱 여덟 아홉 얼마 어디 언제 누가 무엇 이유 경우 문제 방법 부분 상황
있을까 없을까 어떡하죠 어떻게 그래도 아무리 심지어 도대체 벌써 아직 이제 역시 결국 진짜
사람 사람들 생각 얘기 이야기 영상 채널 구독 댓글 감사합니다 감사 선생님 언니 님들 여러분
있습니다 있어요 없어요 합니다 해요 되요 돼요 봅니다 봐요 같아요 같습니다 입니다 이에요""".split())


def tokens(text):
    out = []
    for w in re.findall(r"[가-힣]{2,8}", text or ""):
        for _ in range(2):
            w = ENDING.sub("", w)
            w = JOSA.sub("", w)
        if len(w) >= 2 and w not in STOP and not GRAM.match(w):
            out.append(w)
    return out


TS = re.compile(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b")
# 배지로 따로 보여주므로 본문 맨 앞의 시각은 지운다 — 같은 값이 두 번 보이지 않게
TS_LEAD = re.compile(r"^\s*\d{1,2}:[0-5]\d(?::[0-5]\d)?\s*[-~·]?\s*")


def timestamps(text):
    """댓글에 박힌 재생 시각. 포맷 분석은 '무엇이 먹혔나'까지만 말하지만,
    이건 영상 안 '어디서' 터졌는지를 말해준다."""
    out = []
    for a, b, c in TS.findall(text or ""):
        sec = (int(a) * 3600 + int(b) * 60 + int(c)) if c else (int(a) * 60 + int(b))
        if 0 < sec < 6 * 3600:
            out.append(sec)
    return out


def heatmap(rows, dur, bins=40):
    """구간별 언급 밀도. 좋아요로 가중한다 — 한 사람이 찍은 지점에 200명이
    동의했다면 그 지점의 무게는 1이 아니라 200이다."""
    if not dur:
        dur = max([t for _, t, _ in rows] or [1]) + 30
    h = [0.0] * bins
    for _, sec, likes in rows:
        i = min(bins - 1, int(sec / dur * bins))
        h[i] += 1 + math.log10(1 + max(0, likes)) * 2
    return h, dur


# ---------------------------------------------------------------- brief
# 댓글을 '의미 있는 정보'로 바꾸는 자리. 아웃라이어를 넘어서는 요인은
# 아래 여섯 형태로만 나온다 — 그래서 여섯 축으로만 뽑는다.
AXES = [
    ("peak",    "감정이 터진 지점",  "그 대목의 장치를 내 구조에 심는다"),
    ("unmet",   "채워지지 않은 구멍", "터졌는데도 답 안 한 것 = 내 다음 한 편"),
    ("segment", "빠진 청중",        "원 영상이 안 겨냥한 집단 = 좁지만 확실한 승부처"),
    ("contest", "갈린 각도",        "논쟁 지점 = 내가 더 잘 다룰 자리"),
    ("format",  "만듦새 불만",      "같은 내용을 더 잘 전달할 여지"),
    ("vocab",   "시청자의 말",      "제목·썸네일에 그대로 쓸 표현"),
]


def vid_id(x):
    """URL이든 ID든 받는다."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", x or "")
    return m.group(1) if m else x.strip()


def brief(target, out_path, per=14):
    con = db()
    vid = vid_id(target)
    row = con.execute("SELECT id,title,dur,views,comments FROM video WHERE id=?", (vid,)).fetchone()
    if not row:
        have = con.execute("SELECT id,title FROM video WHERE done=1 ORDER BY views DESC LIMIT 12").fetchall()
        sys.exit("그 영상은 수집되어 있지 않습니다. 먼저 collect 를 돌리세요.\n수집된 영상:\n"
                 + "\n".join(f"  {i}  {t[:52]}" for i, t in have))
    vid, title, dur, views, ncom = row

    cs = con.execute("SELECT text,likes,replies FROM comment WHERE video_id=?", (vid,)).fetchall()
    rich = []
    for text, likes, replies in cs:
        t = (text or "").strip()
        typ, tags = classify(t)
        rich.append({"t": t, "l": likes, "r": replies, "typ": typ, "g": tags,
                     "fmt": bool(RX_FORMAT.search(t)), "ts": timestamps(t)})

    def pick(f, key, k=per, minlen=8):
        seen, out = set(), []
        for d in sorted([x for x in rich if f(x)], key=key, reverse=True):
            sig = re.sub(r"[^가-힣]", "", d["t"])[:18]      # 같은 말 중복 제거
            if len(d["t"]) < minlen or sig in seen:
                continue
            seen.add(sig); out.append(d)
            if len(out) >= k:
                break
        return out

    # 감정 피크
    marks = [(d["t"], s, d["l"]) for d in rich for s in d["ts"]]
    peaks = []
    if len(marks) >= 4:
        bins = 40
        h, dur2 = heatmap(marks, dur, bins)
        slot, used = dur2 / bins, set()
        for i in sorted(range(bins), key=lambda i: -h[i]):
            if h[i] <= 0 or len(peaks) >= 3:
                break
            if i in used:
                continue
            used.update(range(i - 2, i + 3))
            near = sorted([m for m in marks if (i - 1) * slot <= m[1] < (i + 2) * slot],
                          key=lambda m: -m[2])[:5]
            peaks.append((int(i * slot), [(TS_LEAD.sub("", t)[:160], sec, lk) for t, sec, lk in near]))

    # 시청자의 말 — 제목에 없는데 댓글에 반복되는 표현이 곧 제목 후보다
    tl = set(tokens(title))
    vocab = Counter()
    for d in rich:
        if d["typ"] != "react":
            vocab.update({w: 1 + min(d["l"], 50) for w in set(tokens(d["t"])) if w not in tl})

    L = [f"# 아웃라이어 초과 브리프 — 원자료", "",
         f"- 영상: **{title}**", f"- 조회 {views:,} · 댓글 {ncom:,} · 수집 {len(cs):,} · 길이 {dur // 60}:{dur % 60:02d}",
         "", "> 아래는 가공하지 않은 증거입니다. 요약은 뭉개므로 원문 그대로 둡니다.", ""]

    L += ["## 1. 감정이 터진 지점", ""]
    if peaks:
        for at, near in peaks:
            L.append(f"### {at // 60}:{at % 60:02d}")
            L += [f"- ({sec // 60}:{sec % 60:02d}, ♥{lk}) {t}" for t, sec, lk in near]
            L.append("")
    else:
        L += ["- 재생 시각이 박힌 댓글이 거의 없습니다. 이 영상은 특정 대목이 아니라 전체로 소비된 듯합니다.", ""]

    for key, head, hint, f, sortk in [
        ("unmet", "2. 채워지지 않은 구멍 (질문)", "터졌는데도 답 안 한 것",
         lambda d: "question" in d["g"], lambda d: (d["l"], d["r"])),
        ("segment", "3. 빠진 청중 (상황 고백)", "원 영상이 겨냥하지 않은 집단",
         lambda d: "situation" in d["g"], lambda d: (d["l"], d["r"])),
        ("fail", "4. 깨진 지점 (실패 경험)", "기존 해법이 통하지 않은 자리",
         lambda d: "fail" in d["g"], lambda d: (d["l"], d["r"])),
        # 단순 반응에 달린 대댓글은 논쟁이 아니라 동의다. 시각만 찍은 댓글도 마찬가지.
        ("contest", "5. 갈린 각도 (대댓글 많은 댓글)", "의견이 갈린 곳",
         lambda d: d["r"] > 0 and d["typ"] != "react" and not d["ts"],
         lambda d: (d["r"], d["l"])),
        ("format", "6. 만듦새 불만", "같은 내용을 더 잘 전달할 여지",
         lambda d: d["fmt"], lambda d: (d["l"], d["r"])),
    ]:
        L += [f"## {head}", f"*{hint}*", ""]
        got = pick(f, sortk)
        L += [f"- (♥{d['l']} 답{d['r']}) {d['t'][:220]}" for d in got] or ["- 잡힌 것이 없습니다."]
        L.append("")

    L += ["## 7. 시청자의 말 (제목에 없는 반복 표현)",
          "*좋아요로 가중한 빈도. 제목·썸네일 문구 후보입니다.*", "",
          "  ".join(f"`{w}`({n})" for w, n in vocab.most_common(30)), ""]

    body = "\n".join(L)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"브리프 원자료 → {out_path}  ({len(body):,}자)")
    print("\n다음 단계 — 이 파일과 prompts/brief.md 를 함께 LLM에 넘기면")
    print("여섯 축으로 묶인 '무엇을 더 얹을 것인가' 브리프가 나옵니다.")


# ---------------------------------------------------------------- report
def report(out_path, top_n=1500):
    con = db()
    rows = con.execute("""SELECT c.id,c.text,c.likes,c.replies,v.title,c.video_id
                          FROM comment c JOIN video v ON v.id=c.video_id""").fetchall()
    if not rows:
        sys.exit("수집된 댓글이 없습니다. 먼저 collect 를 돌려주세요.")

    items, counts, kw, tagcount = [], Counter(), Counter(), Counter()
    for cid, text, likes, replies, vtitle, vid in rows:
        t, tags = classify(text)
        counts[t] += 1
        tagcount.update(tags)
        if t in CORE or likes >= 3:
            kw.update(set(tokens(text)))
        items.append({"t": t, "g": tags, "x": likes, "y": replies, "c": (text or "").strip()[:400],
                      "v": vtitle, "id": cid, "vid": vid})

    items.sort(key=lambda d: -(d["x"] + d["y"] * 3))
    items = items[:top_n]

    def pct(vals, p):
        if not vals:
            return 0
        s = sorted(vals)
        return s[min(len(s) - 1, int(len(s) * p))]

    tx = max(1, pct([i["x"] for i in items], .75))
    ty = max(1, pct([i["y"] for i in items], .75))

    # 영상별 감정 지도 — 아웃라이어가 '어디서' 터졌는지
    heat = []
    for vid, vtitle, dur, views in con.execute(
            "SELECT id,title,dur,views FROM video WHERE done=1").fetchall():
        marks = []
        for text, likes in con.execute(
                "SELECT text,likes FROM comment WHERE video_id=?", (vid,)):
            for sec in timestamps(text):
                marks.append((text, sec, likes))
        if len(marks) < 4:
            continue
        bins = 40
        h, dur2 = heatmap(marks, dur, bins)
        peaks, used = [], set()
        for i in sorted(range(bins), key=lambda i: -h[i]):
            if h[i] <= 0 or len(peaks) >= 3:
                break
            if i in used:
                continue
            peaks.append(i)
            used.update(range(i - 2, i + 3))   # 좌우 억제: 같은 봉우리를 두 번 세지 않는다
        slot = dur2 / bins
        peak_out = []
        for i in peaks:
            lo, hi = max(0, (i - 1) * slot), (i + 2) * slot
            near = sorted([m for m in marks if lo <= m[1] < hi], key=lambda m: -m[2])[:4]
            peak_out.append({
                "at": int(lo), "w": round(h[i], 1),
                "cs": [{"c": TS_LEAD.sub("", t.strip())[:220], "s": sec, "l": lk}
                       for t, sec, lk in near]})
        heat.append({"id": vid, "t": vtitle, "dur": int(dur2), "n": len(marks),
                     "views": views, "h": [round(x, 2) for x in h], "peaks": peak_out})
    heat.sort(key=lambda d: -d["n"])
    heat = heat[:8]

    total = len(rows)
    core_n = sum(counts[k] for k in CORE)
    nch = con.execute("SELECT COUNT(*) FROM channel").fetchone()[0]
    nvi = con.execute("SELECT COUNT(*) FROM video WHERE done=1").fetchone()[0]

    dist = [{"k": k, "ko": ko, "hint": hint, "n": counts[k],
             "tag": tagcount.get(k, 0), "core": k in CORE}
            for k, ko, hint in TYPES]

    data = {
        "items": items, "dist": dist, "tx": tx, "ty": ty,
        "kw": kw.most_common(40), "heat": heat,
        "stat": {"total": total, "core": core_n, "ch": nch, "vi": nvi,
                 "pctCore": round(core_n / total * 100) if total else 0},
        "types": {k: ko for k, ko, _ in TYPES},
    }
    html = TEMPLATE.replace("%%DATA%%", json.dumps(data, ensure_ascii=False))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"댓글 {total:,}개 분석 → {out_path}")
    print(f"  핵심 재료(실패·상황고백) {core_n:,}개 ({data['stat']['pctCore']}%)")
    print("  " + "-" * 44)
    print(f"  {'유형':<12}{'주':>8}{'겹친 것 포함':>14}")
    for d in dist:
        extra = f"{d['tag']:>14,}" if d["tag"] > d["n"] else f"{'':>14}"
        print(f"  {d['ko']:<12}{d['n']:>8,}{extra}{'  ← 핵심 재료' if d['core'] else ''}")
    print("\n  주 유형은 하나만 잡히므로, 다른 유형에 가려진 신호는 오른쪽 숫자로 봅니다.")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>댓글 마찰 지도</title>
<style>
:root{
  --bg:#f6f5f2; --surface-1:#fcfcfb; --line:#e3e1db;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#78766f;
  --series-1:#2a78d6; --grid:#e8e6e0; --quad:#f1efea;
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
  color-scheme:dark;
  --bg:#121211; --surface-1:#1a1a19; --line:#34332f;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8d8b82;
  --series-1:#3987e5; --grid:#2c2b28; --quad:#201f1d;
}}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#121211; --surface-1:#1a1a19; --line:#34332f;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8d8b82;
  --series-1:#3987e5; --grid:#2c2b28; --quad:#201f1d;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text-primary);
  font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;
  font-size:15px;line-height:1.7;word-break:keep-all}
.wrap{max-width:900px;margin:0 auto;padding:40px 16px 80px}
h1{font-size:clamp(24px,5vw,34px);font-weight:800;letter-spacing:-.02em;margin:0 0 6px}
h2{font-size:19px;font-weight:700;letter-spacing:-.01em;margin:0 0 4px}
.sub{color:var(--text-secondary);font-size:14px;margin:0 0 16px}
.card{background:var(--surface-1);border:1px solid var(--line);border-radius:14px;padding:22px;margin:18px 0}
section{margin:38px 0}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0}
.tile{flex:1;min-width:120px;background:var(--surface-1);border:1px solid var(--line);
  border-radius:12px;padding:14px 16px}
.tile b{display:block;font-size:26px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.tile span{font-size:12.5px;color:var(--text-muted)}
/* bars */
.brow{display:flex;align-items:center;gap:12px;margin:9px 0}
.blab{width:110px;flex:none;font-size:14px}
.btrack{flex:1;height:22px;position:relative}
.bfill{height:100%;background:var(--series-1);border-radius:0 4px 4px 0;min-width:2px}
.bval{width:62px;text-align:right;font-size:13px;color:var(--text-secondary);font-variant-numeric:tabular-nums;flex:none}
.badge{font-size:11px;border:1px solid var(--series-1);color:var(--series-1);
  border-radius:999px;padding:1px 7px;margin-left:6px;vertical-align:1px}
/* scatter */
.plot{position:relative}
svg{display:block;width:100%;height:auto;overflow:visible}
.ax{fill:var(--text-muted);font-size:11px}
.qlab{fill:var(--text-muted);font-size:11.5px;font-weight:600}
.tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .12s;
  background:var(--surface-1);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;max-width:300px;font-size:13px;line-height:1.55;
  box-shadow:0 8px 24px -8px rgba(0,0,0,.28);z-index:5}
.tip b{color:var(--series-1);font-variant-numeric:tabular-nums}
/* chips */
.peek{margin-top:10px;border-top:1px solid var(--line);padding-top:10px;min-height:44px}
.pk{font-size:13.5px;color:var(--text-secondary);margin:5px 0;line-height:1.55}
.pk b{color:var(--series-1);font-variant-numeric:tabular-nums;margin-right:5px}
.pk i{font-style:normal;color:var(--text-muted);font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{border:1px solid var(--line);border-radius:999px;padding:4px 11px;font-size:13px;
  color:var(--text-secondary);background:var(--bg)}
.chip i{font-style:normal;color:var(--text-muted);font-size:11.5px;margin-left:5px;
  font-variant-numeric:tabular-nums}
/* table */
.ctl{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;align-items:center}
button.f,select,input{font-family:inherit;font-size:13.5px;border:1px solid var(--line);
  background:var(--surface-1);color:var(--text-primary);border-radius:9px;padding:7px 12px;cursor:pointer}
button.f.on{background:var(--series-1);border-color:var(--series-1);color:#fff}
input{flex:1;min-width:150px;cursor:text}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-size:12px;color:var(--text-muted);font-weight:600;
  border-bottom:1px solid var(--line);padding:6px 8px;white-space:nowrap}
td{border-bottom:1px solid var(--line);padding:11px 8px;vertical-align:top}
td.n{text-align:right;font-variant-numeric:tabular-nums;color:var(--text-secondary);white-space:nowrap}
.ty{font-size:11.5px;color:var(--text-muted);white-space:nowrap}
.vt{font-size:11.5px;color:var(--text-muted);margin-top:4px}
.more{margin-top:14px}
</style></head><body>
<div class="wrap">
<h1>댓글 마찰 지도</h1>
<p class="sub">감성이 아니라 발화 유형으로 갈랐습니다. 좋아요는 <b>합의</b>, 대댓글은 <b>논쟁</b> — 서로 다른 신호라 축을 나눴습니다.</p>

<div class="stats" id="stats"></div>

<section>
  <h2>무엇을 말하고 있나</h2>
  <p class="sub">찾는 재료는 아래 두 줄에 거의 다 있습니다. "저는 ○○ 때문에 안 돼요"가 곧 문제 정의입니다.</p>
  <div class="card" id="dist"></div>
</section>

<section>
  <h2>합의와 논쟁</h2>
  <p class="sub">가로는 좋아요(같은 문제를 가진 사람 수), 세로는 대댓글(의견이 갈리는 정도). 점 하나가 댓글 하나입니다. 올려보면 원문이 보입니다.</p>
  <div class="card"><div class="plot" id="plot"><div class="tip" id="tip"></div></div></div>
</section>

<section id="heatSec" hidden>
  <h2>어디서 터졌나</h2>
  <p class="sub">댓글에 박힌 재생 시각을 모은 것입니다. 포맷 분석은 <b>무엇이</b> 먹혔는지까지만 말해주지만, 이건 영상 안 <b>어디서</b> 터졌는지를 말해줍니다. 아웃라이어의 성공이 썸네일 때문인지 4분 지점의 그 한 마디 때문인지가 여기서 갈립니다. 막대를 올려보면 그 지점의 댓글이 나옵니다.</p>
  <div id="heat"></div>
</section>

<section>
  <h2>반복되는 말</h2>
  <p class="sub">핵심 재료 댓글과 좋아요 3개 이상 댓글에서만 셌습니다. 형태소 분석 없이 뽑은 거친 신호라 참고용입니다.</p>
  <div class="card"><div class="chips" id="kw"></div></div>
</section>

<section>
  <h2>발굴 순위</h2>
  <p class="sub">좋아요 + 대댓글×3 순. 요약은 뭉개지니 원문 그대로 둡니다. 여기서 같은 말이 세 번 이상 나오면 그게 만들 것입니다.</p>
  <div class="ctl" id="ctl"></div>
  <table><thead><tr><th>댓글</th><th class="n">좋아요</th><th class="n">대댓글</th></tr></thead>
  <tbody id="tb"></tbody></table>
  <div class="more"><button class="f" id="more">더 보기</button></div>
</section>
</div>
<script>
const D = %%DATA%%;
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const nf = n => n.toLocaleString('ko-KR');

/* 통계 타일 */
$('#stats').innerHTML = [
  [nf(D.stat.total), '수집한 댓글'],
  [nf(D.stat.core), '핵심 재료 (실패·상황고백)'],
  [D.stat.pctCore + '%', '핵심 재료 비율'],
  [D.stat.ch + ' / ' + D.stat.vi, '채널 / 영상'],
].map(([v, l]) => `<div class="tile"><b>${v}</b><span>${l}</span></div>`).join('');

/* 유형 분포 — 단일 계열. 색이 아니라 라벨이 정체를 나릅니다. */
const mx = Math.max(...D.dist.map(d => d.n), 1);
$('#dist').innerHTML = D.dist.map(d => `
  <div class="brow" title="${esc(d.hint)}">
    <div class="blab">${esc(d.ko)}${d.core ? '<span class="badge">핵심</span>' : ''}</div>
    <div class="btrack"><div class="bfill" style="width:${(d.n / mx * 100).toFixed(1)}%"></div></div>
    <div class="bval">${nf(d.n)}</div>
  </div>
  <div style="font-size:12.5px;color:var(--text-muted);margin:-6px 0 12px 122px">${esc(d.hint)}${d.tag > d.n ? ` · 겹쳐 나온 것까지 세면 <b style="color:var(--text-secondary)">${nf(d.tag)}</b>` : ''}</div>`).join('');

/* 산점도 — 단일 계열이라 범례 없음. 제목이 계열을 지목합니다. */
const W = 860, H = 440, P = {l: 76, r: 22, t: 24, b: 52};
const L = v => Math.log10(1 + v);
const items = D.items;
const maxX = Math.max(...items.map(d => d.x), 10), maxY = Math.max(...items.map(d => d.y), 5);
const sx = v => P.l + L(v) / L(maxX) * (W - P.l - P.r);
const sy = v => H - P.b - L(v) / L(maxY) * (H - P.t - P.b);
const qx = sx(D.tx), qy = sy(D.ty);
const ticks = m => { const o = [0]; for (let p = 1; p <= m; p *= 10) { o.push(p); if (p * 3 <= m) o.push(p * 3); } return o; };

let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="댓글별 좋아요와 대댓글 분포">`;
s += `<rect x="${qx}" y="${P.t}" width="${W - P.r - qx}" height="${qy - P.t}" fill="var(--quad)"/>`;
s += `<rect x="${qx}" y="${qy}" width="${W - P.r - qx}" height="${H - P.b - qy}" fill="var(--quad)"/>`;
ticks(maxX).forEach(t => { const x = sx(t);
  s += `<line x1="${x}" y1="${P.t}" x2="${x}" y2="${H - P.b}" stroke="var(--grid)" stroke-width="1"/>`;
  s += `<text class="ax" x="${x}" y="${H - P.b + 16}" text-anchor="middle">${nf(t)}</text>`; });
ticks(maxY).forEach(t => { const y = sy(t);
  s += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="var(--grid)" stroke-width="1"/>`;
  s += `<text class="ax" x="${P.l - 9}" y="${y + 4}" text-anchor="end">${nf(t)}</text>`; });
s += `<line x1="${qx}" y1="${P.t}" x2="${qx}" y2="${H - P.b}" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4 4" opacity=".55"/>`;
s += `<line x1="${P.l}" y1="${qy}" x2="${W - P.r}" y2="${qy}" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4 4" opacity=".55"/>`;
s += `<text class="qlab" x="${W - P.r - 6}" y="${P.t + 15}" text-anchor="end">뜨거운 쟁점 · 크고 갈린다</text>`;
s += `<text class="qlab" x="${W - P.r - 6}" y="${qy + 17}" text-anchor="end">합의된 통증 · 가장 안전한 기회</text>`;
s += `<text class="qlab" x="${P.l + 6}" y="${P.t + 15}">소수의 격론 · 깊은 통증</text>`;
items.forEach((d, i) => { s += `<circle data-i="${i}" cx="${sx(d.x).toFixed(1)}" cy="${sy(d.y).toFixed(1)}" r="4" fill="var(--series-1)" fill-opacity=".62" stroke="var(--surface-1)" stroke-width="2"/>`; });
s += `<text class="ax" x="${(P.l + W - P.r) / 2}" y="${H - 6}" text-anchor="middle">좋아요 →  합의</text>`;
s += `<text class="ax" transform="translate(16,${(P.t + H - P.b) / 2}) rotate(-90)" text-anchor="middle">대댓글 →  논쟁</text>`;
s += `</svg>`;
$('#plot').insertAdjacentHTML('afterbegin', s);

/* 조밀한 산점도라 최근접점 방식으로 잡습니다 — 점을 정확히 찍을 필요 없이 */
const svg = $('#plot svg'), tip = $('#tip');
svg.addEventListener('mousemove', e => {
  const r = svg.getBoundingClientRect(), k = W / r.width;
  const mx2 = (e.clientX - r.left) * k, my2 = (e.clientY - r.top) * k;
  let best = -1, bd = 1e9;
  items.forEach((d, i) => { const dx = sx(d.x) - mx2, dy = sy(d.y) - my2, dd = dx * dx + dy * dy;
    if (dd < bd) { bd = dd; best = i; } });
  if (best < 0 || bd > 900) { tip.style.opacity = 0; return; }
  const d = items[best];
  tip.innerHTML = `<div style="margin-bottom:6px">${esc(d.c.slice(0, 180))}${d.c.length > 180 ? '…' : ''}</div>
    <div style="font-size:12px;color:var(--text-muted)">${esc(D.types[d.t])} · 좋아요 <b>${nf(d.x)}</b> · 대댓글 <b>${nf(d.y)}</b></div>`;
  tip.style.opacity = 1;
  tip.style.left = Math.min(r.width - 310, Math.max(0, sx(d.x) / k - 150)) + 'px';
  tip.style.top = Math.max(0, sy(d.y) / k - tip.offsetHeight - 14) + 'px';
});
svg.addEventListener('mouseleave', () => tip.style.opacity = 0);

/* 감정 지도 — 영상마다 한 줄. 단일 계열이라 범례 없이 제목이 계열을 지목합니다. */
const mmss = s => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
if (D.heat && D.heat.length) {
  $('#heatSec').hidden = false;
  const HW = 820, HH = 62, BINS = 40, bw = HW / BINS;
  $('#heat').innerHTML = D.heat.map((v, vi) => {
    const mx = Math.max(...v.h, 1);
    const top = v.peaks[0];
    let g = `<svg viewBox="0 0 ${HW} ${HH + 18}" role="img" aria-label="${esc(v.t)} 구간별 언급 밀도">`;
    v.h.forEach((val, i) => {
      const h2 = Math.max(val > 0 ? 3 : 0, val / mx * HH);
      if (!h2) return;
      g += `<rect data-v="${vi}" data-i="${i}" x="${(i * bw + 1).toFixed(1)}" y="${(HH - h2).toFixed(1)}" `
         + `width="${(bw - 2).toFixed(1)}" height="${h2.toFixed(1)}" rx="3" fill="var(--series-1)" fill-opacity=".8"/>`;
    });
    g += `<line x1="0" y1="${HH}" x2="${HW}" y2="${HH}" stroke="var(--line)" stroke-width="1"/>`;
    [0, .25, .5, .75, 1].forEach(f => {
      g += `<text class="ax" x="${Math.min(HW - 16, Math.max(14, f * HW))}" y="${HH + 14}" text-anchor="middle">${mmss(v.dur * f)}</text>`;
    });
    g += `</svg>`;
    return `<div class="card hv" data-v="${vi}">
      <div style="font-weight:650;font-size:15px;margin-bottom:2px">${esc(v.t)}</div>
      <div style="font-size:12.5px;color:var(--text-muted);margin-bottom:10px">
        조회 ${nf(v.views)} · 시각 언급 ${nf(v.n)}건 · 길이 ${mmss(v.dur)}
        ${top ? ` · 가장 뜨거운 지점 <b style="color:var(--series-1)">${mmss(top.at)}</b>` : ''}</div>
      ${g}
      <div class="peek" id="peek${vi}">${top ? top.cs.map(c =>
        `<div class="pk"><b>${mmss(c.s)}</b> ${esc(c.c)}${c.l ? ` <i>♥${nf(c.l)}</i>` : ''}</div>`).join('') : ''}</div>
    </div>`;
  }).join('');
  $('#heat').addEventListener('mousemove', e => {
    const r = e.target.closest('rect[data-i]'); if (!r) return;
    const v = D.heat[+r.dataset.v], i = +r.dataset.i, slot = v.dur / BINS;
    const pk = v.peaks.find(p => Math.abs(p.at - i * slot) < slot);
    const box = document.getElementById('peek' + r.dataset.v);
    if (pk) box.innerHTML = pk.cs.map(c =>
      `<div class="pk"><b>${mmss(c.s)}</b> ${esc(c.c)}${c.l ? ` <i>♥${nf(c.l)}</i>` : ''}</div>`).join('');
  });
}

/* 반복되는 말 */
$('#kw').innerHTML = D.kw.map(([w, n]) => `<span class="chip">${esc(w)}<i>${n}</i></span>`).join('');

/* 표 — 색이 아닌 텍스트로 정체를 나르는 접근 가능한 뷰 */
let filt = 'core', shown = 40, q = '';
$('#ctl').innerHTML =
  `<button class="f on" data-f="core">핵심 재료만</button>` +
  D.dist.map(d => `<button class="f" data-f="${d.k}">${esc(d.ko)}</button>`).join('') +
  `<button class="f" data-f="all">전체</button><input id="q" placeholder="단어로 검색">`;
function view() {
  return items.filter(d => (filt === 'all' || (filt === 'core' ? (d.g.includes('fail') || d.g.includes('situation')) : d.g.includes(filt)))
    && (!q || d.c.includes(q)));
}
function draw() {
  const v = view();
  $('#tb').innerHTML = v.slice(0, shown).map(d => `<tr>
    <td>${esc(d.c)}<div class="vt">${esc(D.types[d.t])} · ${esc(d.v)}</div></td>
    <td class="n">${nf(d.x)}</td><td class="n">${nf(d.y)}</td></tr>`).join('')
    || `<tr><td colspan="3" style="color:var(--text-muted)">해당하는 댓글이 없습니다.</td></tr>`;
  $('#more').style.display = v.length > shown ? '' : 'none';
  $('#more').textContent = `더 보기 (${nf(v.length - shown)}개 남음)`;
}
$('#ctl').addEventListener('click', e => { const b = e.target.closest('button.f'); if (!b) return;
  document.querySelectorAll('#ctl button.f').forEach(x => x.classList.toggle('on', x === b));
  filt = b.dataset.f; shown = 40; draw(); });
$('#q').addEventListener('input', e => { q = e.target.value.trim(); shown = 40; draw(); });
$('#more').addEventListener('click', () => { shown += 60; draw(); });
draw();
</script></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="유튜브 댓글에서 마찰을 캐낸다")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect"); c.add_argument("targets", nargs="+",
        help="채널(@핸들 또는 UC…) 또는 영상 URL/ID. 섞어 써도 됩니다")
    c.add_argument("--videos", type=int, default=30, help="채널당 최근 영상 수 (기본 30)")
    r = sub.add_parser("report"); r.add_argument("--out", default="dashboard.html")
    b = sub.add_parser("brief"); b.add_argument("video", help="영상 URL 또는 ID")
    b.add_argument("--out", default="brief_input.md")
    # 유튜브 ID는 '-'로 시작할 수 있다(-2fagsF-gzo). 그대로 두면 argparse가
    # 옵션으로 오해하므로 URL 꼴로 바꿔서 넘긴다. vid_id 가 다시 풀어낸다.
    argv = [f"https://youtu.be/{t}" if re.fullmatch(r"-[A-Za-z0-9_-]{10}", t) else t
            for t in sys.argv[1:]]
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        # 채널이면 채널 경로, 영상 URL/ID면 영상 경로로 자동 분기
        chans = [t for t in a.targets if t.startswith("@") or
                 (t.startswith("UC") and len(t) == 24)]
        vids = [t for t in a.targets if t not in chans]
        if chans:
            collect(chans, a.videos)
        if vids:
            collect_videos(vids)
    elif a.cmd == "brief":
        brief(a.video, a.out)
    else:
        report(a.out)
