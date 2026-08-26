# -*- coding: utf-8 -*-
"""역사 실화극장 — 클라우드 자동 발행 (GitHub Actions에서 실행).

PC 전원과 무관하게 정해진 시각에 유튜브 + 인스타에 동시 발행한다.
표준 라이브러리만 쓴다. 토큰은 전부 환경변수(GitHub Secrets)에서 읽는다.

환경변수
  YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN   유튜브 OAuth
  IG_ACCESS_TOKEN                                       인스타 장기 토큰
  DRY_RUN=1                                             발행 안 하고 대상만 출력
  TZ=Asia/Seoul                                         at 시각 판정 기준 (워크플로에서 지정)

저장소 파일
  posts.json           발행 큐 (at 지난 승인 편만 대상)
  reels/NNN.mp4        유튜브 업로드용 (여기 체크아웃돼 있음)
  thumbs/NNN.jpg       유튜브 썸네일
  yt_sent.json         유튜브 발행 기록 (content_id 키) — 실행 후 커밋됨
  ig_sent.json         인스타 발행 기록 (content_id 키) — 실행 후 커밋됨
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POSTS = ROOT / "posts.json"
YT_SENT = ROOT / "yt_sent.json"
IG_SENT = ROOT / "ig_sent.json"

DRY = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status")
CATEGORY_EDU = "27"
CHUNK = 1024 * 1024 * 4

IG_API = "https://graph.instagram.com/v23.0"
IG_POLL_INTERVAL = 5
IG_POLL_TIMEOUT = 300
IG_THUMB_OFFSET_MS = 1200


def log(msg):
    print(msg, flush=True)


def load_json(p, default):
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def need_env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit("[설정 오류] 환경변수 %s 가 없습니다 (GitHub Secrets 확인)." % name)
    return v


# ----------------------------------------------------------------- 유튜브 --

def yt_client():
    """YT_CLIENT_SECRET_JSON = client_secret.json 파일 통째. client_id/secret 추출."""
    try:
        d = json.loads(need_env("YT_CLIENT_SECRET_JSON"))
    except json.JSONDecodeError:
        raise SystemExit("[유튜브] YT_CLIENT_SECRET_JSON 이 올바른 JSON 이 아닙니다 "
                         "(client_secret.json 전체를 붙여넣으세요).")
    conf = d.get("installed") or d.get("web")
    if not conf:
        raise SystemExit("[유튜브] client_secret.json 형식 오류 (installed/web 없음).")
    return conf["client_id"], conf["client_secret"]


def yt_refresh_token():
    """YT_TOKEN_JSON = yt_token.json 파일 통째. refresh_token 추출."""
    try:
        d = json.loads(need_env("YT_TOKEN_JSON"))
    except json.JSONDecodeError:
        raise SystemExit("[유튜브] YT_TOKEN_JSON 이 올바른 JSON 이 아닙니다 "
                         "(yt_token.json 전체를 붙여넣으세요).")
    if "refresh_token" not in d:
        raise SystemExit("[유튜브] yt_token.json 에 refresh_token 이 없습니다.")
    return d["refresh_token"]


def yt_access_token():
    cid, csec = yt_client()
    t = _post_form(TOKEN_URL, {
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": yt_refresh_token(),
        "grant_type": "refresh_token"})
    if "access_token" not in t:
        raise SystemExit("[유튜브] 토큰 갱신 실패: %s" % t.get("_error", t))
    return t["access_token"]


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": "%s %s" % (e.code, e.read().decode("utf-8", "replace"))}


def yt_meta(p):
    title = p["title"][:99]
    desc = (p.get("caption", "") + "\n\n#Shorts")[:4900]
    tags = [w[1:] for w in p.get("caption", "").split() if w.startswith("#")][:15]
    return {
        "snippet": {"title": title, "description": desc, "tags": tags,
                    "categoryId": CATEGORY_EDU},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }


def yt_upload(cid, p, token):
    mp4 = ROOT / "reels" / ("%s.mp4" % cid)
    if not mp4.exists():
        raise SystemExit("[유튜브] 영상 없음: %s" % mp4)
    meta = yt_meta(p)
    size = mp4.stat().st_size
    log("  [유튜브] 업로드 시작: %s (%.1fMB) '%s'"
        % (cid, size / 1048576.0, meta["snippet"]["title"]))

    body = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(UPLOAD_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json; charset=UTF-8")
    req.add_header("X-Upload-Content-Length", str(size))
    req.add_header("X-Upload-Content-Type", "video/mp4")
    with urllib.request.urlopen(req, timeout=120) as r:
        session = r.headers.get("Location")
    if not session:
        raise SystemExit("[유튜브] 업로드 세션 URL 없음")

    sent = 0
    result = None
    with mp4.open("rb") as f:
        while sent < size:
            chunk = f.read(CHUNK)
            end = sent + len(chunk) - 1
            r = urllib.request.Request(session, data=chunk, method="PUT")
            r.add_header("Content-Length", str(len(chunk)))
            r.add_header("Content-Range", "bytes %d-%d/%d" % (sent, end, size))
            try:
                with urllib.request.urlopen(r, timeout=300) as resp:
                    result = json.loads(resp.read().decode())
                    sent = size
            except urllib.error.HTTPError as e:
                if e.code == 308:
                    sent = end + 1
                    continue
                raise SystemExit("[유튜브] 전송 실패 %s: %s"
                                 % (e.code, e.read().decode("utf-8", "replace")))
    vid = result.get("id")
    url = "https://www.youtube.com/shorts/%s" % vid
    yt_set_thumbnail(cid, vid, token)
    log("  [유튜브] 완료: %s" % url)
    return {"video_id": vid, "url": url,
            "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "privacy": "public"}


def yt_set_thumbnail(cid, video_id, token):
    thumb = ROOT / "thumbs" / ("%s.jpg" % cid)
    if not (thumb.exists() and thumb.stat().st_size <= 2 * 1024 * 1024):
        log("  [유튜브] 썸네일 파일 없음/초과 — 자동 프레임으로 둠")
        return
    url = ("https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=%s"
           % video_id)
    req = urllib.request.Request(url, data=thumb.read_bytes(), method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "image/jpeg")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
        log("  [유튜브] 썸네일 등록: thumbs/%s.jpg" % cid)
    except urllib.error.HTTPError as e:
        e.read()
        log("  [유튜브] 썸네일 등록 실패 (%s)" % e.code)


# ----------------------------------------------------------------- 인스타 --

def ig_get(path, token, **params):
    params["access_token"] = token
    url = "%s/%s?%s" % (IG_API, path.lstrip("/"), urllib.parse.urlencode(params))
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s — %s"
                           % (e.code, e.read().decode("utf-8", "replace")[:300]))


def ig_post(path, token, **params):
    params["access_token"] = token
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request("%s/%s" % (IG_API, path.lstrip("/")),
                                 data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit("[인스타] API 오류 %s: %s"
                         % (e.code, e.read().decode("utf-8", "replace")))


def ig_user_id(token):
    m = ig_get("me", token, fields="user_id,username")
    return m.get("user_id") or m.get("id")


def ig_publish(cid, p, token):
    uid = ig_user_id(token)
    log("  [인스타] 컨테이너 생성: %s" % cid)
    c = ig_post("%s/media" % uid, token,
                media_type="REELS",
                video_url=p["video_url"],
                caption=p.get("caption", ""),
                thumb_offset=str(p.get("thumb_offset_ms", IG_THUMB_OFFSET_MS)),
                share_to_feed="true")
    creation_id = c["id"]
    waited = 0
    while waited < IG_POLL_TIMEOUT:
        d = ig_get(creation_id, token, fields="status_code,status")
        code = d.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise SystemExit("[인스타] 영상 처리 실패: %s" % d.get("status", ""))
        time.sleep(IG_POLL_INTERVAL)
        waited += IG_POLL_INTERVAL
    else:
        raise SystemExit("[인스타] 처리 시간 초과 (%ds)" % IG_POLL_TIMEOUT)
    mid = ig_post("%s/media_publish" % uid, token, creation_id=creation_id)["id"]
    log("  [인스타] 완료: media_id=%s" % mid)
    return {"media_id": mid, "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}


# --------------------------------------------------------------- 첫 댓글 --
# 발행 직후 운영자 첫 댓글을 자동으로 단다. 문구는 comments_text.json(편별).
COMMENTS_TEXT = ROOT / "comments_text.json"


def comment_text_for(cid):
    return load_json(COMMENTS_TEXT, {}).get(cid)


def yt_comment(video_id, text, token):
    body = json.dumps({"snippet": {"videoId": video_id,
                       "topLevelComment": {"snippet": {"textOriginal": text}}}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet",
        data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json; charset=UTF-8")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def ig_comment(media_id, text, token):
    return ig_post("%s/comments" % media_id, token, message=text)


# ------------------------------------------------------------------- main --

def due_items(posts, yt_sent, ig_sent, now):
    out = []
    for p in posts:
        cid, at = p.get("content_id"), p.get("at")
        if not cid or not at:
            continue
        if cid in yt_sent and cid in ig_sent:
            continue
        try:
            at_dt = datetime.strptime(at, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if at_dt <= now:
            out.append((at_dt, cid, p))
    out.sort(key=lambda x: x[0])
    return out


def main():
    now = datetime.now()
    posts = load_json(POSTS, [])
    yt_sent = load_json(YT_SENT, {})
    ig_sent = load_json(IG_SENT, {})

    due = due_items(posts, yt_sent, ig_sent, now)
    log("=== 역사 실화극장 클라우드 발행 %s (KST) ===" % now.strftime("%Y-%m-%d %H:%M"))
    log("대상 %d편: %s" % (len(due), ", ".join(c for _, c, _ in due) or "(없음)"))

    if DRY:
        log("\n[DRY_RUN] 발행하지 않고 자격만 검증합니다.")
        try:
            yt_access_token()
            log("  [유튜브] 토큰 OK")
        except SystemExit as e:
            log("  [유튜브] 토큰 실패: %s" % e)
        try:
            uid = ig_user_id(need_env("IG_ACCESS_TOKEN"))
            log("  [인스타] 토큰 OK (user_id=%s)" % uid)
        except Exception as e:
            log("  [인스타] 토큰 실패: %s" % e)
        log("[DRY_RUN] 종료.")
        return 0

    if not due:
        log("발행할 예약 편이 없습니다.")
        return 0

    yt_token = yt_access_token()
    ig_token = need_env("IG_ACCESS_TOKEN")

    changed = False
    for at_dt, cid, p in due:
        log("\n--- %s (%s)" % (cid, at_dt.strftime("%m-%d %H:%M")))
        msg = comment_text_for(cid)
        if cid not in yt_sent:
            try:
                yt_sent[cid] = yt_upload(cid, p, yt_token)
                save_json(YT_SENT, yt_sent)
                changed = True
                if msg:
                    try:
                        r = yt_comment(yt_sent[cid]["video_id"], msg, yt_token)
                        log("  [유튜브] 첫 댓글 완료 (id=%s)" % r.get("id"))
                    except (Exception, SystemExit) as e:
                        log("  [유튜브] 첫 댓글 실패: %s" % e)
            except (SystemExit, RuntimeError) as e:
                log("  %s — 다음 실행에 재시도" % e)
        else:
            log("  [유튜브] 이미 발행됨")
        if cid not in ig_sent:
            try:
                ig_sent[cid] = ig_publish(cid, p, ig_token)
                save_json(IG_SENT, ig_sent)
                changed = True
                if msg:
                    try:
                        r = ig_comment(ig_sent[cid]["media_id"], msg, ig_token)
                        log("  [인스타] 첫 댓글 완료 (id=%s)" % r.get("id"))
                    except (Exception, SystemExit) as e:
                        log("  [인스타] 첫 댓글 실패: %s" % e)
            except (SystemExit, RuntimeError) as e:
                log("  %s — 다음 실행에 재시도" % e)
        else:
            log("  [인스타] 이미 발행됨")

    log("\n=== 완료 (변경 %s) ===" % ("있음" if changed else "없음"))
    # GitHub Actions에 커밋 필요 여부 전달
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write("changed=%s\n" % ("true" if changed else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
