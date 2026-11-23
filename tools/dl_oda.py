"""多线程分块下载 ODA File Converter MSI（带签名直链，60秒有效，需即时获取）。"""
import concurrent.futures, urllib.request, os, sys, time

PAGE = "https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_vc16_amd64dll.msi"
OUT = "tools/ODAFileConverter.msi"
TOTAL = 33656320
N = 12


def get_url():
    return open("tools/oda_url.txt", encoding="utf-8").read().strip()


def fetch(args):
    idx, url, start, end = args
    dest = f"{OUT}.part{idx}"
    want = end - start + 1
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
            data = urllib.request.urlopen(req, timeout=60).read()
            if len(data) == want:
                with open(dest, "wb") as f:
                    f.write(data)
                return idx, len(data)
        except Exception:
            time.sleep(1)
    return idx, 0


def main():
    url = get_url()
    print("got signed url, downloading...", flush=True)
    size = (TOTAL + N - 1) // N
    tasks = []
    for i in range(N):
        s = i * size
        e = min(s + size - 1, TOTAL - 1)
        if s <= e:
            tasks.append((i, url, s, e))
    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
        for idx, n in ex.map(fetch, tasks):
            ok += n
            print(f"  part{idx}: {n}", flush=True)
    with open(OUT, "wb") as out:
        for i in range(len(tasks)):
            with open(f"{OUT}.part{i}", "rb") as f:
                out.write(f.read())
    print("DONE size:", os.path.getsize(OUT), "expected:", TOTAL, flush=True)
    for i in range(len(tasks)):
        try:
            os.remove(f"{OUT}.part{i}")
        except OSError:
            pass


if __name__ == "__main__":
    main()
