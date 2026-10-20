#!/usr/bin/env python3
"""🚀 DEPLOY ROBUST — Tom 19/10: triệt để vụ web kẹt.

Nguyên nhân gốc đã xác minh: đường git tới github.com qua proxy hay treo/403 từng đợt,
mà cách kiểm tra cũ (ls-remote/status) cũng chết theo → báo "ahead" ảo, push retry
mù mờ. Token GitHub (gh) vẫn sống — vấn đề là transport + verification, không phải auth.

Cơ chế 2 đường + tự kiểm chứng bằng LIVE SITE:
  1. git push (đường chính), timeout chặt
  2. verify: remote HEAD == local HEAD (ls-remote, retry)
  3. nếu vẫn lệch → FALLBACK API: gh contents PUT index.html + version.json
     (api.github.com đi đường khác, đã chứng minh ổn định hơn qua proxy)
  4. verify cuối: live version.json trên Pages == local version.json (poll 120s)

Exit: 0 = live đã khớp bản mới (hoặc vốn đã khớp); 1 = đã đẩy nhưng chưa verify được live;
        2 = thất bại cả 2 đường.
KHÔNG commit gì trong working tree — chỉ đẩy commit đã có.
Chống chạy chồng: flock trên /tmp/deploy-board.lock.
"""
import base64, fcntl, json, os, subprocess, sys, time, urllib.request

REPO = os.path.expanduser("~/workspace/market-board")
REPO_SLUG = "kevinsea9-dev/bang-market-live"
LIVE_BASE = "https://kevinsea9-dev.github.io/bang-market-live"
LOCK = "/tmp/deploy-board.lock"
FILES = ["index.html", "version.json"]


def run(*a, timeout=60):
    try:
        r = subprocess.run(a, cwd=REPO, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


def local_head():
    rc, out, _ = run("git", "rev-parse", "HEAD")
    return out if rc == 0 else None


def remote_head(retries=3):
    for _ in range(retries):
        rc, out, _ = run("git", "ls-remote", "origin", "main", timeout=45)
        if rc == 0 and out:
            return out.split()[0]
        time.sleep(10)
    return None


def local_version():
    try:
        return json.load(open(os.path.join(REPO, "version.json")))["v"]
    except Exception:
        return None


def live_version():
    try:
        url = f"{LIVE_BASE}/version.json?t={int(time.time())}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                  "Cache-Control": "no-cache"})
        d = json.load(urllib.request.urlopen(req, timeout=20))
        return d.get("v")
    except Exception:
        return None


def api_push_file(path):
    """PUT 1 file lên main qua Contents API. Trả True nếu thành công."""
    full = os.path.join(REPO, path)
    data = open(full, "rb").read()
    b64 = base64.b64encode(data).decode()
    rc, out, err = run("gh", "api", f"repos/{REPO_SLUG}/contents/{path}",
                       "--jq", ".sha", timeout=30)
    sha = out if rc == 0 and out else None
    # FIX 19/10 ~22:25 PT: base64 index.html (~165KB) vượt MAX_ARG_STRLEN
    # khi nhét vào argv → OSError "Argument list too long". Gửi body qua
    # stdin (--input -) thay vì -f CLI args.
    body = {"message": f"deploy: {path} (fallback API)",
            "content": b64, "branch": "main"}
    if sha:
        body["sha"] = sha
    try:
        p = subprocess.run(["gh", "api", f"repos/{REPO_SLUG}/contents/{path}",
                            "-X", "PUT", "--input", "-"],
                           input=json.dumps(body), cwd=REPO,
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print(f"deploy: API PUT {path} timeout", file=sys.stderr)
        return False
    if p.returncode != 0:
        print(f"deploy: API PUT {path} FAILED: {p.stderr.strip()[:120]}",
              file=sys.stderr)
        return False
    print(f"deploy: API PUT {path} OK")
    return True


def api_fallback():
    """Đẩy 2 file qua API rồi đồng bộ HEAD local. Trả True nếu xong."""
    for f in FILES:
        if not api_push_file(f):
            return False
    run("git", "fetch", "origin", timeout=60)
    rc, out, _ = run("git", "diff", "--name-only", "origin/main...HEAD", timeout=30)
    extra = [x for x in out.splitlines() if x not in FILES]
    rc2, out2, _ = run("git", "diff", "--quiet", "HEAD", "origin/main", "--",
                       *FILES, timeout=30)
    if extra or rc2 != 0:
        # local còn commit khác chưa deploy — không reset, báo để xử lý tay
        print("deploy: API xong nhưng local còn file khác chưa deploy, KHÔNG reset",
              file=sys.stderr)
        return False
    run("git", "reset", "--mixed", "origin/main", timeout=30)
    print("deploy: HEAD local đã đồng bộ theo commit API")
    return True


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("deploy: lượt khác đang chạy — thoát")
        return 0
    try:
        return _main()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)


def _main():
    lh = local_head()
    if not lh:
        print("deploy: không đọc được HEAD", file=sys.stderr)
        return 2
    # FIX 19/10 ~22:25 PT: live site là ground truth (bài học gốc 19/10).
    # Trước đây điều kiện còn đòi rh == lh — khi git transport treo
    # (ls-remote trả None) vẫn tưởng "lệch" rồi đốt ~270s push vô ích +
    # lao vào fallback dù chẳng có gì mới.
    lv = local_version()
    if lv is not None and live_version() == lv:
        print(f"deploy: đã đồng bộ (live v={lv}) — không làm gì")
        return 0
    rh = remote_head()

    pushed = False
    for i in range(3):
        rc, out, err = run("git", "push", "origin", "main", timeout=90)
        if rc == 0:
            pushed = True
            break
        print(f"deploy: git push lần {i+1}/3 thất bại ({err[:80]})", file=sys.stderr)
        time.sleep(15)

    rh = remote_head()
    if rh != lh:
        print("deploy: git push không ăn thua → fallback API", file=sys.stderr)
        if not api_fallback():
            print("deploy: FAILED cả 2 đường", file=sys.stderr)
            return 2
        lh = local_head()

    # verify cuối bằng LIVE SITE (ground truth của người xem)
    for _ in range(8):
        if live_version() == local_version():
            print(f"deploy: VERIFIED live v={local_version()} ✓")
            return 0
        time.sleep(15)
    print("deploy: đã đẩy nhưng live chưa khớp (Pages lag?)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
