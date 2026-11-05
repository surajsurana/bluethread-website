#!/usr/bin/env python3
"""Bluethread Instagram auto-poster (@bluethreadai.in).

Standard library only. Uses the Instagram API with Unified (Facebook) Login
(graph.facebook.com) and a never-expiring system-user token from the Bluethread
business portfolio, so there is no token refresh.

Modes:
  --dry-run            Validate queue.json, check image URLs return 200, print plan.
  --dry-run --local    Same, but check local files instead of URLs (before deploy).
  --run                Post every item that is due AND approved (used by the workflow).
  --approve MONTH|ID   Mark items approved and lock them with an approval hash.
  --check-token        Read-only: confirm the token can see the Instagram account.
  --rehearse [ID]      Full rehearsal of the next (or given) post without publishing it.

Safety rules enforced in --run:
  * Only items with approved == true AND a matching approval_hash post. Any edit to
    caption, images, alt text or time after approval breaks the hash, so it will not
    post until it is approved again.
  * Items post in scheduled order, at most one per run.
  * An item more than max_late_minutes past its time is not posted late: it and every
    later item move forward by whole days to the next open slot (logged in
    queue["postponements"]). Nothing ever blocks the queue.
  * A failed publish stays pending and is retried by the next run (a later trigger
    the same evening, or the postponement above).
  * Before publishing, recent account media is checked for the same caption, so a
    re-run after a crash does not double-post.

Secrets come only from env vars IG_USER_ID and IG_ACCESS_TOKEN.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{API_VERSION}"
SITE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
QUEUE_PATH = os.path.join(SITE_ROOT, "social", "queue.json")
STATUSES = {"pending", "posted", "failed"}
MAX_CAPTION = 2200
MAX_HASHTAGS = 30
MAX_ALT = 1000
MAX_CAROUSEL = 20
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class QueueError(Exception):
    pass


# ---------------------------------------------------------------- queue

def load_queue():
    with open(QUEUE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_queue(q):
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, QUEUE_PATH)


def parse_time(s):
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise QueueError(f"scheduled_at '{s}' has no timezone (use +05:30 for IST)")
    return t


def file_sha(rel):
    with open(os.path.join(SITE_ROOT, rel), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def approval_hash(post):
    """Hash of everything that will appear on Instagram, including image bytes."""
    payload = {
        "id": post["id"],
        "scheduled_at": post["scheduled_at"],
        "caption": post["caption"],
        "images": [[p, file_sha(p)] for p in post["images"]],
        "alt_text": post.get("alt_text") or [],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def validate(q):
    errors = []
    posts = q.get("posts")
    if not isinstance(posts, list) or not posts:
        return ["queue has no posts"]
    ids = set()
    for p in posts:
        pid = p.get("id", "?")
        where = f"[{pid}]"
        if pid in ids:
            errors.append(f"{where} duplicate id")
        ids.add(pid)
        for k in ("id", "scheduled_at", "images", "caption", "approved", "status"):
            if k not in p:
                errors.append(f"{where} missing '{k}'")
        if errors and errors[-1].startswith(where):
            continue
        try:
            parse_time(p["scheduled_at"])
        except (ValueError, QueueError) as e:
            errors.append(f"{where} bad scheduled_at: {e}")
        if p["status"] not in STATUSES:
            errors.append(f"{where} bad status '{p['status']}'")
        if not isinstance(p["approved"], bool):
            errors.append(f"{where} approved must be true/false")
        imgs = p["images"]
        if not isinstance(imgs, list) or not (1 <= len(imgs) <= MAX_CAROUSEL):
            errors.append(f"{where} images must be a list of 1-{MAX_CAROUSEL}")
            continue
        for rel in imgs:
            if not rel.lower().endswith((".jpg", ".jpeg")):
                errors.append(f"{where} {rel}: Instagram API accepts JPEG only")
            full = os.path.join(SITE_ROOT, rel)
            if not os.path.isfile(full):
                errors.append(f"{where} {rel}: file not found")
            elif os.path.getsize(full) > MAX_IMAGE_BYTES:
                errors.append(f"{where} {rel}: over 8 MB")
        alts = p.get("alt_text") or []
        if alts and len(alts) != len(imgs):
            errors.append(f"{where} alt_text must have one entry per image")
        for a in alts:
            if len(a) > MAX_ALT:
                errors.append(f"{where} alt text over {MAX_ALT} chars")
        cap = p["caption"]
        if len(cap) > MAX_CAPTION:
            errors.append(f"{where} caption over {MAX_CAPTION} chars")
        if cap.count("#") > MAX_HASHTAGS:
            errors.append(f"{where} more than {MAX_HASHTAGS} hashtags")
        if p["approved"] and p["status"] == "pending":
            try:
                if p.get("approval_hash") != approval_hash(p):
                    errors.append(f"{where} approved but content changed since approval "
                                  "(approval_hash mismatch) - needs a new approval")
            except OSError:
                pass  # missing file already reported
    return errors


def image_url(q, rel):
    return q["base_url"].rstrip("/") + "/" + rel.lstrip("/")


def url_ok(url):
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "bluethread-ig-poster"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        return f"error: {e.reason}"


# ---------------------------------------------------------------- Instagram API

def api(method, path, params, token, retries=3):
    params = dict(params)
    params["access_token"] = token
    data = urllib.parse.urlencode(params).encode()
    url = f"{GRAPH}/{path}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + data.decode(), method="GET")
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            # Never let a token leak into the log via an echoed URL.
            body = body.replace(token, "***")
            if 500 <= e.code < 600 and attempt < retries - 1:
                last = body
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"Instagram API {method} {path} -> HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            last = str(e.reason)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Instagram API {method} {path} failed: {last}")


def wait_finished(cid, token, max_wait=300):
    deadline = time.time() + max_wait
    while True:
        st = api("GET", cid, {"fields": "status_code,status"}, token).get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {cid} status {st}")
        if time.time() > deadline:
            raise RuntimeError(f"container {cid} not FINISHED after {max_wait}s (last {st})")
        time.sleep(10)


def create_image_container(user, token, url, alt, extra):
    params = {"image_url": url, **extra}
    if alt:
        params["alt_text"] = alt
    try:
        return api("POST", f"{user}/media", params, token)["id"]
    except RuntimeError as e:
        # alt_text is documented for /media but not confirmed for this login type.
        # If it is the field that is rejected, retry once without it (alt text is
        # accessibility-only, not visible on the post).
        if alt and "alt_text" in str(e):
            print(f"  WARNING: alt_text rejected, retrying without it: {e}")
            params.pop("alt_text")
            return api("POST", f"{user}/media", params, token)["id"]
        raise


def already_published(user, token, caption):
    """Return the media id if a recent post already has this exact caption."""
    res = api("GET", f"{user}/media", {"fields": "id,caption,timestamp", "limit": "25"}, token)
    for m in res.get("data", []):
        if (m.get("caption") or "").strip() == caption.strip():
            return m["id"]
    return None


def publish(q, post, user, token):
    urls = [image_url(q, r) for r in post["images"]]
    alts = post.get("alt_text") or [None] * len(urls)
    for u in urls:
        s = url_ok(u)
        if s != 200:
            raise RuntimeError(f"image not reachable ({s}): {u}")
    if len(urls) == 1:
        cid = create_image_container(user, token, urls[0], alts[0], {"caption": post["caption"]})
    else:
        children = []
        for u, a in zip(urls, alts):
            ch = create_image_container(user, token, u, a, {"is_carousel_item": "true"})
            wait_finished(ch, token)
            children.append(ch)
        cid = api("POST", f"{user}/media", {"media_type": "CAROUSEL",
                                            "children": ",".join(children),
                                            "caption": post["caption"]}, token)["id"]
    wait_finished(cid, token)
    post["container_id"] = cid
    return api("POST", f"{user}/media_publish", {"creation_id": cid}, token)["id"]


# ---------------------------------------------------------------- commands

def ordered(q):
    return sorted(q["posts"], key=lambda p: parse_time(p["scheduled_at"]))


def cmd_dry_run(q, local):
    errors = validate(q)
    now = dt.datetime.now(dt.timezone.utc)
    print(f"Queue: {QUEUE_PATH}\nNow (UTC): {now:%Y-%m-%d %H:%M}\n")
    for p in ordered(q):
        t = parse_time(p["scheduled_at"])
        kind = "single" if len(p["images"]) == 1 else f"carousel x{len(p['images'])}"
        flag = "APPROVED" if p["approved"] else "not approved"
        due = "due" if t <= now else "future"
        print(f"- {p['id']}  {p['scheduled_at']}  {kind}  {p['status']}  {flag}  ({due})")
        for i, rel in enumerate(p["images"]):
            if local:
                ok = os.path.isfile(os.path.join(SITE_ROOT, rel))
                status = "local file OK (URL not checked: not yet deployed)" if ok else "MISSING"
            else:
                status = url_ok(image_url(q, rel))
            print(f"    {image_url(q, rel)}  -> {status}")
            if not local and status != 200:
                errors.append(f"[{p['id']}] {image_url(q, rel)} returned {status}")
        first = p["caption"].splitlines()[0] if p["caption"] else ""
        print(f"    caption: {len(p['caption'])} chars, {p['caption'].count('#')} hashtags: \"{first}\"")
        if p["approved"] and p["status"] == "pending" and t <= now:
            print("    WOULD POST NOW (if hash matches and earlier posts are done)")
    print()
    if errors:
        print("ERRORS:")
        for e in errors:
            print("  " + e)
        return 1
    print("Dry run OK. Nothing was sent to Instagram.")
    return 0


def postpone(q, first, now, late_limit):
    """Move `first` and every later unposted item forward by the same whole number of
    days, to the first slot that is not already missed. Keeps the daily rhythm and the
    grid order. A post's approval survives the move only if its content still matches
    what was approved (hash checked at the old time, then re-locked at the new time)."""
    t = parse_time(first["scheduled_at"])
    days = 1
    while now - (t + dt.timedelta(days=days)) > late_limit:
        days += 1
    moved = []
    for p in ordered(q):
        if p["status"] == "posted" or parse_time(p["scheduled_at"]) < t:
            continue
        intact = p["approved"] and p.get("approval_hash") == approval_hash(p)
        p["scheduled_at"] = (parse_time(p["scheduled_at"]) + dt.timedelta(days=days)).isoformat()
        p["status"] = "pending"
        p["error"] = None
        if intact:
            p["approval_hash"] = approval_hash(p)
        elif p["approved"]:
            p["approved"] = False  # content changed since approval: never post it unseen
        moved.append(p["id"])
    note = (f"{first['id']} missed its slot; it and {len(moved) - 1} later post(s) "
            f"moved {days} day(s) later")
    q.setdefault("postponements", []).append(
        {"at": now.isoformat(timespec="seconds"), "days": days, "posts": moved})
    print(note)
    return parse_time(first["scheduled_at"])


def cmd_run(q):
    errors = validate(q)
    if errors:
        print("Queue invalid, refusing to post:\n  " + "\n  ".join(errors))
        return 1
    user = os.environ.get("IG_USER_ID")
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not user or not token:
        print("IG_USER_ID / IG_ACCESS_TOKEN not set")
        return 1
    now = dt.datetime.now(dt.timezone.utc)
    late_limit = dt.timedelta(minutes=int(q.get("max_late_minutes", 180)))
    for p in ordered(q):
        if p["status"] == "posted":
            continue
        t = parse_time(p["scheduled_at"])
        # A missed slot (or a post that failed on an earlier run and is now past its
        # window) never blocks the queue: everything moves forward together.
        if now - t > late_limit:
            t = postpone(q, p, now, late_limit)
            save_queue(q)
        if t > now:
            print(f"{p['id']}: next post, due {p['scheduled_at']}; nothing to do yet.")
            return 0
        if not p["approved"]:
            print(f"{p['id']}: due but NOT approved; holding it and everything after it.")
            return 0
        print(f"{p['id']}: posting ({len(p['images'])} image(s))")
        try:
            existing = already_published(user, token, p["caption"])
            media_id = existing or publish(q, p, user, token)
            if existing:
                print(f"  already on Instagram as {existing}; recording it, not reposting")
        except Exception as e:  # noqa: BLE001 - keep it pending so the next run retries
            p["error"] = str(e)[:1000]
            save_queue(q)
            print(f"  FAILED (will retry on the next run): {e}")
            return 1
        p.update(status="posted", ig_media_id=media_id, error=None,
                 posted_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        save_queue(q)
        print(f"  published: media id {media_id}")
        return 0  # at most one post per run
    print("queue empty: every post is published")
    return 0


def cmd_rehearse(q, target):
    """Everything a real post does except the final publish call: validate, check the
    images are public, have Instagram fetch them into containers, wait until the
    containers are ready, and confirm the token holds the publish permission.
    Unpublished containers are never shown and expire on their own after 24 hours."""
    errors = validate(q)
    if errors:
        print("Queue invalid:\n  " + "\n  ".join(errors))
        return 1
    user, token = os.environ.get("IG_USER_ID"), os.environ.get("IG_ACCESS_TOKEN")
    if not user or not token:
        print("IG_USER_ID / IG_ACCESS_TOKEN not set")
        return 1
    pending = [p for p in ordered(q) if p["status"] != "posted"]
    p = next((x for x in pending if x["id"] == target), None) if target else (pending or [None])[0]
    if not p:
        print("no such pending post")
        return 1
    if not p["approved"]:
        print(f"{p['id']}: not approved")
        return 1
    me = api("GET", user, {"fields": "username,media_count"}, token)
    print(f"token OK: @{me.get('username')}, {me.get('media_count')} posts")
    lim = api("GET", f"{user}/content_publishing_limit",
              {"fields": "config,quota_usage"}, token).get("data", [{}])[0]
    print(f"publish permission OK: used {lim.get('quota_usage')} of "
          f"{(lim.get('config') or {}).get('quota_total')} posts in the last 24 h")
    if already_published(user, token, p["caption"]):
        print(f"{p['id']}: already on Instagram")
        return 1
    print(f"{p['id']}: building containers ({len(p['images'])} image(s)), not publishing")
    urls = [image_url(q, r) for r in p["images"]]
    alts = p.get("alt_text") or [None] * len(urls)
    for u in urls:
        s = url_ok(u)
        if s != 200:
            print(f"image not reachable ({s}): {u}")
            return 1
    if len(urls) == 1:
        cid = create_image_container(user, token, urls[0], alts[0], {"caption": p["caption"]})
    else:
        children = []
        for u, a in zip(urls, alts):
            ch = create_image_container(user, token, u, a, {"is_carousel_item": "true"})
            wait_finished(ch, token)
            print(f"  image ready: {u}")
            children.append(ch)
        cid = api("POST", f"{user}/media", {"media_type": "CAROUSEL",
                                            "children": ",".join(children),
                                            "caption": p["caption"]}, token)["id"]
    wait_finished(cid, token)
    print(f"  post container {cid} FINISHED: ready to publish. NOT published (rehearsal).")
    print("REHEARSAL OK")
    return 0


def cmd_approve(q, target):
    n = 0
    for p in q["posts"]:
        if p["status"] != "pending":
            continue
        if p["id"] == target or p["scheduled_at"].startswith(target):
            p["approved"] = True
            p["approval_hash"] = approval_hash(p)
            p["approved_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            n += 1
            print(f"approved {p['id']}")
    if not n:
        print(f"no pending posts match '{target}'")
        return 1
    save_queue(q)
    return 0


def cmd_check_token():
    user, token = os.environ.get("IG_USER_ID"), os.environ.get("IG_ACCESS_TOKEN")
    if not user or not token:
        print("IG_USER_ID / IG_ACCESS_TOKEN not set")
        return 1
    res = api("GET", user, {"fields": "username,media_count"}, token)
    print(f"token OK: @{res.get('username')}, {res.get('media_count')} posts")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--run", action="store_true")
    g.add_argument("--approve", metavar="MONTH_OR_ID", help="e.g. 2026-11 or a post id")
    g.add_argument("--check-token", action="store_true")
    g.add_argument("--rehearse", nargs="?", const="", metavar="ID")
    ap.add_argument("--local", action="store_true", help="with --dry-run: check local files, not URLs")
    a = ap.parse_args()
    if a.check_token:
        return cmd_check_token()
    q = load_queue()
    if a.dry_run:
        return cmd_dry_run(q, a.local)
    if a.approve:
        return cmd_approve(q, a.approve)
    if a.rehearse is not None:
        return cmd_rehearse(q, a.rehearse)
    return cmd_run(q)


if __name__ == "__main__":
    sys.exit(main())
