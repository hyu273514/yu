import csv, hashlib, io, json, pathlib, re, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
from bs4 import BeautifulSoup
from pypdf import PdfReader

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ROWS = json.load(open("pubmed_linkout_queue.json", encoding="utf-8"))
OUT = pathlib.Path("pubmed_linkout_retry_results")
PDFROOT = OUT / "pdf"
PDFROOT.mkdir(parents=True, exist_ok=True)


def safe(value, limit=150):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or ""))
    return re.sub(r"\s+", "_", value).strip("._ ")[:limit] or "untitled"


def words(value):
    stop = {"with", "from", "that", "this", "into", "after", "using", "study", "review", "clinical", "patients", "guideline"}
    return {word for word in re.findall(r"[a-z0-9]+", str(value).lower()) if len(word) > 3 and word not in stop}


def validate(data, title):
    if not data.lstrip().startswith(b"%PDF-"):
        return None, "not_pdf"
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        pages = len(reader.pages)
        text = " ".join((reader.pages[index].extract_text() or "") for index in range(min(3, pages)))
        score = len(words(title) & words(text)) / max(1, len(words(title)))
        if text and score < 0.20:
            return None, f"mismatch:{score:.2f}"
        return (pages, len(data), hashlib.sha256(data).hexdigest(), score), ""
    except Exception as exc:
        return None, f"pdf_error:{type(exc).__name__}:{str(exc)[:100]}"


def collect_urls(obj, found):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in {"url", "urlname"} and isinstance(value, str) and value.startswith("http"):
                found.append(value)
            collect_urls(value, found)
    elif isinstance(obj, list):
        for value in obj:
            collect_urls(value, found)


def query_linkout(row):
    session = requests.Session()
    session.headers.update({"User-Agent": "ENT-Literature-Archive/1.0 (mailto:alice246991@gmail.com)"})
    api = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&id={row['pmid']}&cmd=llinks&retmode=json&tool=ent_literature_archive&email=alice246991%40gmail.com"
    errors = []
    for attempt in range(6):
        try:
            response = session.get(api, timeout=(10, 40))
            if response.status_code == 429:
                delay = 1.5 * (attempt + 1)
                errors.append(f"429_retry_{attempt + 1}")
                time.sleep(delay)
                continue
            response.raise_for_status()
            urls = []
            collect_urls(response.json(), urls)
            urls = [url for url in dict.fromkeys(urls) if "static.pubmed.gov" not in url and "nih.gov/corehtml" not in url]
            return {**row, "linkout_urls": urls, "api_error": " | ".join(errors)}
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:140]}")
            time.sleep(1.5 * (attempt + 1))
    return {**row, "linkout_urls": [], "api_error": " | ".join(errors)}


# Query NCBI sequentially to stay below rate limits.
queried = []
for index, row in enumerate(ROWS, 1):
    item = query_linkout(row)
    queried.append(item)
    print(f"LinkOut [{index}/{len(ROWS)}] PMID{row['pmid']} URLs={len(item['linkout_urls'])}", flush=True)
    time.sleep(0.55)


def html_links(response):
    links = []
    try:
        soup = BeautifulSoup(response.text, "html.parser")
        for meta in soup.select('meta[name="citation_pdf_url"],meta[name="wkhealth_pdf_url"],meta[property="og:pdf"]'):
            if meta.get("content"):
                links.append(urllib.parse.urljoin(response.url, meta["content"].strip()))
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if ".pdf" in href.lower() or "/pdf/" in href.lower() or "/epdf/" in href.lower():
                links.append(urllib.parse.urljoin(response.url, href))
    except Exception:
        pass
    return list(dict.fromkeys(links))[:30]


def download(item):
    row = item
    title = row["title"]
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 ENT-Literature-Archive/1.0", "Accept": "text/html,application/pdf,*/*"})
    candidates = list(row["linkout_urls"])
    seen = set()
    errors = [row.get("api_error", "")]
    result = {**row, "status": "failed", "method": "", "source_url": "", "path": "", "pages": "", "size_bytes": "", "sha256": "", "match_score": "", "error": ""}
    index = 0
    while index < len(candidates) and index < 80:
        url = candidates[index]
        index += 1
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            response = session.get(url, timeout=(10, 45), allow_redirects=True, verify=False)
            info, error = validate(response.content, title)
            if info:
                path = PDFROOT / f"{row['pmid']}_{safe(title)}.pdf"
                path.write_bytes(response.content)
                result.update(status="success", method="PubMed_LinkOut_retry", source_url=response.url, path=str(path), pages=info[0], size_bytes=info[1], sha256=info[2], match_score=round(info[3], 3), error="")
                return result
            content_type = (response.headers.get("content-type") or "").lower()
            if "html" in content_type or response.text[:20].lstrip().startswith("<"):
                candidates.extend(html_links(response))
            errors.append(f"{response.status_code}:{content_type}:{error}:{response.url}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}:{url}")
    result["error"] = " | ".join(error for error in errors if error)[:15000]
    return result


results = []
with ThreadPoolExecutor(max_workers=5) as executor:
    futures = {executor.submit(download, item): item for item in queried}
    for index, future in enumerate(as_completed(futures), 1):
        result = future.result()
        results.append(result)
        print(f"Download [{index}/{len(queried)}] {result['status']} PMID{result['pmid']} {result.get('pages', '')}", flush=True)

fields = []
for result in results:
    for key in result:
        if key not in fields:
            fields.append(key)
with open(OUT / "report.csv", "w", encoding="utf-8-sig", newline="") as file:
    writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for result in results:
        row = dict(result)
        row["linkout_urls"] = " | ".join(row.get("linkout_urls", [])) if isinstance(row.get("linkout_urls"), list) else row.get("linkout_urls", "")
        writer.writerow(row)
summary = {"total": len(results), "success": sum(result["status"] == "success" for result in results), "failed": sum(result["status"] != "success" for result in results), "pages": sum(int(result.get("pages") or 0) for result in results), "bytes": sum(int(result.get("size_bytes") or 0) for result in results)}
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(summary)
