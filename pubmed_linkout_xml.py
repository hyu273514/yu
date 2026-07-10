import csv, hashlib, io, json, pathlib, re, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree as ET

import requests
import urllib3
from bs4 import BeautifulSoup
from pypdf import PdfReader

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ROWS = json.load(open("pubmed_linkout_queue.json", encoding="utf-8"))
OUT = pathlib.Path("pubmed_linkout_xml_results")
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


def ncbi_get(session, url):
    errors = []
    for attempt in range(6):
        try:
            response = session.get(url, timeout=(10, 40), allow_redirects=True)
            if response.status_code == 429:
                errors.append(f"429_retry_{attempt+1}")
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response, " | ".join(errors)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}")
            time.sleep(1.5 * (attempt + 1))
    return None, " | ".join(errors)


def get_linkout(row):
    session = requests.Session()
    session.headers.update({"User-Agent": "ENT-Literature-Archive/1.0 (mailto:alice246991@gmail.com)"})
    pmid = row["pmid"]
    xml_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&id={pmid}&cmd=llinks&retmode=xml&tool=ent_literature_archive&email=alice246991%40gmail.com"
    response, api_error = ncbi_get(session, xml_url)
    urls = []
    raw_xml = ""
    if response is not None:
        raw_xml = response.text
        try:
            root = ET.fromstring(response.content)
            for obj in root.findall(".//ObjUrl"):
                url_node = obj.find("Url")
                if url_node is not None and url_node.text and url_node.text.startswith("http"):
                    urls.append(url_node.text.strip())
            for node in root.findall(".//Url"):
                if node.text and node.text.startswith("http"):
                    urls.append(node.text.strip())
        except Exception as exc:
            api_error += f" | XMLParse:{type(exc).__name__}:{str(exc)[:100]}"
    # The prlinks endpoint itself is a candidate; fetching it follows the primary full-text redirect.
    primary = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&id={pmid}&cmd=prlinks&retmode=ref&tool=ent_literature_archive&email=alice246991%40gmail.com"
    urls.append(primary)
    return {**row, "linkout_urls": list(dict.fromkeys(urls)), "api_error": api_error, "linkout_xml_excerpt": raw_xml[:3000]}


queried = []
for index, row in enumerate(ROWS, 1):
    item = get_linkout(row)
    queried.append(item)
    print(f"XML [{index}/{len(ROWS)}] PMID{row['pmid']} URLs={len(item['linkout_urls'])}", flush=True)
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
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 ENT-Literature-Archive/1.0", "Accept": "text/html,application/pdf,*/*"})
    title = item["title"]
    candidates = list(item["linkout_urls"])
    result = {**item, "status": "failed", "method": "", "source_url": "", "path": "", "pages": "", "size_bytes": "", "sha256": "", "match_score": "", "error": ""}
    errors = [item.get("api_error", "")]
    seen = set()
    index = 0
    while index < len(candidates) and index < 100:
        url = candidates[index]
        index += 1
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            response = session.get(url, timeout=(10, 50), allow_redirects=True, verify=False)
            info, error = validate(response.content, title)
            if info:
                path = PDFROOT / f"{item['pmid']}_{safe(title)}.pdf"
                path.write_bytes(response.content)
                result.update(status="success", method="PubMed_LinkOut_XML", source_url=response.url, path=str(path), pages=info[0], size_bytes=info[1], sha256=info[2], match_score=round(info[3], 3), error="")
                return result
            content_type = (response.headers.get("content-type") or "").lower()
            if "html" in content_type or response.text[:20].lstrip().startswith("<"):
                candidates.extend(html_links(response))
            errors.append(f"{response.status_code}:{content_type}:{error}:{response.url}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}:{str(exc)[:120]}:{url}")
    result["error"] = " | ".join(error for error in errors if error)[:18000]
    return result


results = []
with ThreadPoolExecutor(max_workers=5) as executor:
    futures = {executor.submit(download, item): item for item in queried}
    for index, future in enumerate(as_completed(futures), 1):
        result = future.result()
        results.append(result)
        print(f"Download [{index}/{len(queried)}] {result['status']} PMID{result['pmid']} {result.get('pages','')}", flush=True)

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
        if isinstance(row.get("linkout_urls"), list):
            row["linkout_urls"] = " | ".join(row["linkout_urls"])
        writer.writerow(row)
summary = {"total": len(results), "success": sum(result["status"] == "success" for result in results), "failed": sum(result["status"] != "success" for result in results), "pages": sum(int(result.get("pages") or 0) for result in results), "bytes": sum(int(result.get("size_bytes") or 0) for result in results)}
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(summary)
