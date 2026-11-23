"""并行分块下载 ODA File Converter MSI（官方签名直链 60 秒过期，先取链再立即并发）。
全部走 curl，复用系统代理配置。"""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = "https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_vc16_amd64dll.msi"
OUT = ROOT / "tools" / "ODAFileConverter_fast.msi"
CHUNK = 1024 * 1024
WORKERS = 8


_URL_LOCK = threading.Lock()
_URL_CACHE = {"url": "", "time": 0.0}


def fresh_url() -> str:
    with _URL_LOCK:
        if _URL_CACHE["url"] and time.time() - _URL_CACHE["time"] < 20:
            return _URL_CACHE["url"]
        txt = subprocess.check_output(["curl", "-sIL", "-m", "20", PAGE], text=True, errors="ignore")
        locs = [ln.strip().split(":", 1)[1].strip() for ln in txt.splitlines()
                if ln.lower().startswith("location:")]
        if not locs:
            raise RuntimeError("no redirect location")
        _URL_CACHE["url"] = locs[-1]
        _URL_CACHE["time"] = time.time()
        return _URL_CACHE["url"]


def _fresh_url_uncached() -> str:
    txt = subprocess.check_output(["curl", "-sIL", "-m", "20", PAGE], text=True, errors="ignore")
    locs = [ln.strip().split(":", 1)[1].strip() for ln in txt.splitlines()
            if ln.lower().startswith("location:")]
    if not locs:
        raise RuntimeError("no redirect location")
    return locs[-1]


def head_size(url: str) -> int:
    # 代理可能隐藏 HEAD 的 Content-Length，用 1 字节 Range 请求取 Content-Range
    txt = subprocess.check_output(
        ["curl", "-s", "-m", "30", "-r", "0-0", "-D", "-", "-o", os.devnull, url],
        text=True, errors="ignore")
    for ln in txt.splitlines():
        if ln.lower().startswith("content-range:"):
            return int(ln.split("/", 1)[1].strip())
    for ln in txt.splitlines():
        if ln.lower().startswith("content-length:"):
            return int(ln.split(":", 1)[1].strip())
    return 0


def fetch(url: str, idx: int, start: int, end: int, out: Path):
    part = out.with_name(out.name + f".part{idx:02d}")
    expected = end - start + 1
    if part.exists() and part.stat().st_size == expected:
        return idx, expected
    for attempt in range(10):
        url = _fresh_url_uncached()   # 签名 60 秒过期，每次尝试前换新链
        try:
            data = subprocess.check_output(
                ["curl", "-s", "--http1.1", "-m", "180", "-r", f"{start}-{end}", url])
        except subprocess.CalledProcessError as ex:
            print(f"part {idx:02d} attempt {attempt} curl error {ex.returncode}", flush=True)
            time.sleep(1)
            continue
        if len(data) == expected:
            part.write_bytes(data)
            return idx, expected
        print(f"part {idx:02d} attempt {attempt} got {len(data)} != {expected}", flush=True)
        time.sleep(1)
    raise RuntimeError(f"part {idx} download failed")


def main():
    url = fresh_url()
    total = head_size(url)
    print(f"url acquired, total={total} bytes, workers={WORKERS}", flush=True)
    n = (total + CHUNK - 1) // CHUNK
    ranges = [(i, i * CHUNK, min(total - 1, (i + 1) * CHUNK - 1)) for i in range(n)]
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(fetch, url, i, s, e, OUT) for i, s, e in ranges]
        for f in concurrent.futures.as_completed(futs):
            i, size = f.result()
            print(f"part {i:02d} done {size}", flush=True)
    with open(OUT, "wb") as w:
        for i, _, _ in ranges:
            w.write(OUT.with_name(OUT.name + f".part{i:02d}").read_bytes())
    print(f"FINAL {OUT} {OUT.stat().st_size} bytes in {time.time()-t0:.1f}s", flush=True)
    for i, _, _ in ranges:
        OUT.with_name(OUT.name + f".part{i:02d}").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
